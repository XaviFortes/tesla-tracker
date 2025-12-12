import os
import json
import asyncio
import logging
import httpx
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters, ConversationHandler, CallbackQueryHandler
from functools import wraps
from inventory import InventoryManager
from option_codes import OPTION_CODES_DATA
import uuid

# --- Configuration ---
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
DB_FILE = '/data/tesla_users_v2.json'  # New DB file for multi-user
CLIENT_ID = 'ownerapi'
TOKEN_URL = 'https://auth.tesla.com/oauth2/v3/token'
APP_VERSION = '4.48.1-3479'

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=LOG_LEVEL
)
logger = logging.getLogger(__name__)

# --- Decoders & Data (Re-used) ---

FACTORY_CODES = {'F': 'Fremont', 'C': 'Shanghai', 'B': 'Berlin', 'A': 'Austin'}

def decode_vin(vin):
    if not vin or len(vin) != 17: return None
    plant = FACTORY_CODES.get(vin[10], "Unknown Factory")
    year_map = {'M': 2021, 'N': 2022, 'P': 2023, 'R': 2024, 'S': 2025, 'T': 2026}
    year = year_map.get(vin[9], "Unknown Year")
    return f"{plant} ({year})"

def get_image_url(options, model_code):
    opts_clean = [c for c in options if c != '']
    opt_string = ",".join(opts_clean)
    model = 'm3' if 'mdl3' in model_code.lower() else 'my'
    if 'model3' in model_code.lower(): model = 'm3'
    if 'modely' in model_code.lower(): model = 'my'
    return f"https://static-assets.tesla.com/configurator/compositor?model={model}&options={opt_string}&view=STUD_3QTR&size=1920&bkba_opt=1&crop=1400,850,300,300"

# --- Database Manager ---
class UserDatabase:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.users = {} # {chat_id (str): UserDict}
        self.load()

    def load(self):
        if os.path.exists(DB_FILE):
            try:
                with open(DB_FILE, 'r') as f:
                    self.users = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load DB: {e}")
                self.users = {}

    def save(self):
        """Sync save (should be quick for small DB). Call in executor if large."""
        temp = DB_FILE + '.tmp'
        with open(temp, 'w') as f:
            json.dump(self.users, f)
        os.replace(temp, DB_FILE)

    async def get_user(self, chat_id):
        async with self.lock:
            return self.users.get(str(chat_id))

    async def update_user(self, chat_id, data):
        async with self.lock:
            chat_id = str(chat_id)
            if chat_id not in self.users:
                self.users[chat_id] = {}
            self.users[chat_id].update(data)
            self.save()

    async def delete_user(self, chat_id):
        async with self.lock:
            if str(chat_id) in self.users:
                del self.users[str(chat_id)]
                self.save()
            
    async def get_all_users(self):
        async with self.lock:
            return list(self.users.keys())

    def add_watch(self, chat_id, criteria):
        user = self.users.get(str(chat_id))
        if not user: return None
        
        if 'watches' not in user: user['watches'] = []
        
        watch_id = str(uuid.uuid4())[:8]
        criteria['id'] = watch_id
        user['watches'].append(criteria)
        self.save()
        return watch_id

    def remove_watch(self, chat_id, watch_id):
        user = self.users.get(str(chat_id))
        if not user or 'watches' not in user: return False
        
        initial = len(user['watches'])
        user['watches'] = [w for w in user['watches'] if w['id'] != watch_id]
        if len(user['watches']) < initial:
            self.save()
            return True
        return False

# --- Tesla Client (Per User) ---
class TeslaClient:
    def __init__(self, chat_id, db: UserDatabase):
        self.chat_id = chat_id
        self.db = db
        self.headers = {'User-Agent': f'TeslaApp/{APP_VERSION}', 'X-Tesla-User-Agent': f'TeslaApp/{APP_VERSION}'}

    async def _get_token(self):
        user = await self.db.get_user(self.chat_id)
        if not user or 'refresh_token' not in user:
            raise Exception("User not logged in.")
        
        # Check if we assume access token is valid or just try and catch 401
        # Simple strategy: Try with current, refresh on 401
        return user.get('access_token'), user['refresh_token']

    async def _refresh(self, refresh_token):
        async with httpx.AsyncClient() as client:
            payload = {
                'grant_type': 'refresh_token',
                'client_id': CLIENT_ID,
                'refresh_token': refresh_token,
                'scope': 'openid email offline_access'
            }
            resp = await client.post(TOKEN_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
            
            # Update DB with new tokens
            await self.db.update_user(self.chat_id, {
                'access_token': data['access_token'],
                'refresh_token': data['refresh_token']
            })
            return data['access_token']

    async def request(self, method, url):
        access_token, refresh_token = await self._get_token()
        
        if not access_token:
            access_token = await self._refresh(refresh_token)
            
        async with httpx.AsyncClient() as client:
            try:
                if method == 'GET':
                    resp = await client.get(url, headers={**self.headers, 'Authorization': f'Bearer {access_token}'})
                
                if resp.status_code == 401:
                    logger.info(f"Token expired for user {self.chat_id}, refreshing...")
                    access_token = await self._refresh(refresh_token)
                    # Retry once
                    if method == 'GET':
                        resp = await client.get(url, headers={**self.headers, 'Authorization': f'Bearer {access_token}'})
                
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    raise Exception("Auth Failed: Token Expired and Refresh Failed. Please /login again.")
                raise

    async def get_orders(self):
        url = 'https://owner-api.teslamotors.com/api/1/users/orders'
        data = await self.request('GET', url)
        return data.get('response', [])

    async def get_order_details(self, order_id):
        url = f'https://akamai-apigateway-vfx.tesla.com/tasks?deviceLanguage=en&deviceCountry=US&referenceNumber={order_id}&appVersion={APP_VERSION}'
        return await self.request('GET', url)

# --- Decorators & Error Handling ---

def check_auth(func):
    """Decorator to enforce login"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat_id = update.effective_chat.id
        db = context.bot_data['db']
        user = await db.get_user(chat_id)
        if not user or 'refresh_token' not in user:
            await update.message.reply_text("⚠️ **Not Authorized**\nPlease log in first:\n`/login <refresh_token>`", parse_mode='Markdown')
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the user."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            f"❌ **An error occurred:**\n`{context.error}`",
            parse_mode='Markdown'
        )

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Unknown command. Try `/help`.")

# --- Commands ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 **Tesla Tracker Commands**\n\n"
        "🔐 **Setup**\n"
        "`/login <refresh_token>` - Authorize bot (Revokes old tokens!)\n"
        "`/logout` - Remove your data\n"
        "`/interval <minutes>` - Set check frequency (default 30m)\n\n"
        "🚘 **Inspect** (Login Required)\n"
        "`/status` - Full report of all orders\n"
        "`/vin` - Only VIN & Factory info\n"
        "`/options` - Decoded configuration code\n"
        "`/image` - Get vehicle render\n\n"
        "🔍 **Inventory Watch** (Login Required)\n"
        "`/inv_test` - Test inventory API connection\n"
        "`/inv_watch model=my market=ES` - Start Watch Wizard\n"
        "`/inv_check` - Run immediate check\n"
        "`/inv_list` - List active watches\n"
        "`/inv_del <id>` - Delete watch\n"
        "`/inv_edit <id>` - Edit watch\n\n"
        "ℹ️ *Your refresh token is stored locally on your server.*"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
    
    if not args:
        await update.message.reply_text("Usage: `/login <your_refresh_token>`\nRun `python get_initial_token.py` locally to generate one.", parse_mode='Markdown')
        return

    refresh_token = args[0]
    db = context.bot_data['db']
    
    status_msg = await update.message.reply_text("🔐 Validating token with Tesla...")
    
    try:
        # Save momentarily
        await db.update_user(chat_id, {'refresh_token': refresh_token, 'access_token': None, 'interval': 30})
        
        client = TeslaClient(chat_id, db)
        orders = await client.get_orders()
        
        await status_msg.edit_text(f"✅ Success! Found {len(orders)} orders.\nPolling started (30m interval).")
        start_job(context.job_queue, chat_id, 30*60)
        
        try: await update.message.delete()
        except: pass 
            
    except Exception as e:
        await db.delete_user(chat_id)
        err_msg = str(e)
        if "401" in err_msg or "Auth Failed" in err_msg:
            err_msg = "Token Expired or Invalid. Please generate a new one."
        await status_msg.edit_text(f"❌ Login Failed: {err_msg}")

@check_auth
async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = context.bot_data['db']
    
    await db.delete_user(chat_id)
    
    jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in jobs: job.schedule_removal()

    # Also remove inventory jobs
    inv_jobs = context.job_queue.get_jobs_by_name(f"inv_{chat_id}")
    for job in inv_jobs: job.schedule_removal()
        
    await update.message.reply_text("👋 Logged out. Data removed.")

@check_auth
async def interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
    db = context.bot_data['db']
    
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: `/interval <minutes>` (e.g., `/interval 60`)", parse_mode='Markdown')
        return
        
    minutes = int(args[0])
    if minutes < 5:
        await update.message.reply_text("❌ Minimum interval is 5 minutes.")
        return
        
    await db.update_user(chat_id, {'interval': minutes})
    start_job(context.job_queue, chat_id, minutes*60)
    await update.message.reply_text(f"✅ Polling interval set to {minutes} minutes.")

# --- Specific Feature Commands ---

@check_auth
async def generic_info_command(update: Update, context, mode):
    chat_id = update.effective_chat.id
    db = context.bot_data['db']
    client = TeslaClient(chat_id, db)
    
    try:
        orders = await client.get_orders()
        if not orders:
            await update.message.reply_text("No orders found.")
            return

        for order in orders:
            rn = order['referenceNumber']
            details = await client.get_order_details(rn)
            
            if mode == 'vin':
                vin = order.get('vin')
                intel = decode_vin(vin)
                await update.message.reply_text(f"🚗 **{rn}**\nVIN: `{vin or 'None'}`\nFactory: {intel or 'Unknown'}", parse_mode='Markdown')
            
            elif mode == 'options':
                codes = order.get('optionCodeList', [])
                decoded = []
                for c in codes:
                    if c in OPTION_CODES: decoded.append(f"`{c}`: {OPTION_CODES[c]}")
                desc = "\n".join(decoded) or "No known options."
                await update.message.reply_text(f"🧬 **{rn} Configuration**\n{desc}", parse_mode='Markdown')
            
            elif mode == 'image':
                url = get_image_url(order.get('optionCodeList', []), order.get('modelCode', 'my'))
                await update.message.reply_photo(url, caption=f"📸 {rn}")

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def vin_command(update, context): await generic_info_command(update, context, 'vin')
async def options_command(update, context): await generic_info_command(update, context, 'options')
async def image_command(update, context): await generic_info_command(update, context, 'image')

# --- Inventory Commands ---

@check_auth
async def inv_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test connectivity to Tesla Inventory API"""
    status = await update.message.reply_text("📡 Testing connection to Inventory API (Spain)...")
    inv = context.bot_data['inventory']
    
    # Test criteria
    results = await inv.check_inventory({'market': 'ES', 'model': 'my', 'condition': 'new'})
    
    if results:
        await status.edit_text(f"✅ **Success!** Found {len(results)} cars in Spain public inventory.\nYour server IP is NOT blocked.")
    else:
        await status.edit_text("❌ **Failed.** Tesla API returned 0 results or error.\nYour IP might be blocked (403).")

# --- Wizard Conversation States ---
SELECT_MODEL, SELECT_MARKET, MAIN_MENU, SET_PRICE, SET_OPTION, SELECT_CONDITION = range(6)

# --- Wizard Handlers ---

@check_auth
async def start_watch_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /inv_watch"""
    
    # If args provided, try old legacy mode for power users/scripts
    if context.args:
        return await legacy_inv_watch(update, context)

    # Initialize session
    context.user_data['watch_config'] = {
        'model': 'my', 'market': 'ES', 'condition': 'new', 'options': [], 'price': None
    }
    
    keyboard = [
        [InlineKeyboardButton("Model Y", callback_data="my"), InlineKeyboardButton("Model 3", callback_data="m3")],
        [InlineKeyboardButton("Model S", callback_data="ms"), InlineKeyboardButton("Model X", callback_data="mx")]
    ]
    await update.message.reply_text("🔎 **New Inventory Watch**\n\nSelect Model:", 
                                    reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECT_MODEL

async def select_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    context.user_data['watch_config']['model'] = query.data
    
    # Common markets
    keyboard = [
        [InlineKeyboardButton("🇪🇸 ES", callback_data="ES"), InlineKeyboardButton("🇫🇷 FR", callback_data="FR")],
        [InlineKeyboardButton("🇩🇪 DE", callback_data="DE"), InlineKeyboardButton("🇮🇹 IT", callback_data="IT")],
        [InlineKeyboardButton("🇳🇱 NL", callback_data="NL"), InlineKeyboardButton("🇳🇴 NO", callback_data="NO")],
        [InlineKeyboardButton("🇺🇸 US", callback_data="US"), InlineKeyboardButton("🇨🇦 CA", callback_data="CA")]
    ]
    await query.edit_message_text(f"Selected: **Model {query.data.upper()}**\n\nSelect Market:", 
                                  reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SELECT_MARKET

async def select_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['watch_config']['market'] = query.data
    return await show_main_menu(query, context)

async def show_main_menu(query, context):
    cfg = context.user_data['watch_config']
    
    # Summary
    price_str = f"{cfg['price']} EUR" if cfg['price'] else "Any"
    opts_str = ", ".join(cfg['options']) if cfg['options'] else "None"
    
    # Condition Label
    c_mode = cfg.get('condition_mode', 'all_new')
    c_map = {
        'all_new': 'New (All)',
        'brand_new': 'Brand New Only',
        'demo': 'Demo Only',
        'used': 'Used'
    }
    cond_str = c_map.get(c_mode, c_mode)
    
    text = (
        f"⚙️ **Watch Configuration**\n"
        f"• Model: `{cfg['model']}`\n"
        f"• Market: `{cfg['market']}`\n"
        f"• Condition: `{cond_str}`\n"
        f"• Price Limit: `{price_str}`\n"
        f"• Filters: `{opts_str}`\n\n"
        f"Select an action:"
    )
    
    keyboard = [
        [InlineKeyboardButton("💰 Set Max Price", callback_data="action_price")],
        [InlineKeyboardButton("📋 Set Condition", callback_data="action_condition")],
        [InlineKeyboardButton("🎨 Add Filters (Paint/Wheels...)", callback_data="action_filter")],
        [InlineKeyboardButton("✅ Start Watch", callback_data="action_save")],
        [InlineKeyboardButton("❌ Cancel", callback_data="action_cancel")]
    ]
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return MAIN_MENU

async def show_condition_menu(query, context):
    keyboard = [
        [InlineKeyboardButton("✨ All New", callback_data="mode_all_new")],
        [InlineKeyboardButton("🆕 Brand New Only", callback_data="mode_brand_new")],
        [InlineKeyboardButton("🏎️ Demo Only", callback_data="mode_demo")],
        [InlineKeyboardButton("♻️ Used", callback_data="mode_used")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ]
    await query.edit_message_text("Select Condition:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_CONDITION

async def select_condition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "back_main":
        return await show_main_menu(query, context)
        
    # data is mode_brand_new etc
    mode = data.replace("mode_", "")
    
    # Logic:
    # used -> condition='used', mode='used'
    # others -> condition='new', mode=...
    
    if mode == 'used':
        context.user_data['watch_config']['condition'] = 'used'
        context.user_data['watch_config']['condition_mode'] = 'used'
    else:
        context.user_data['watch_config']['condition'] = 'new'
        context.user_data['watch_config']['condition_mode'] = mode
        
    return await show_main_menu(query, context)

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "action_save":
        return await save_watch(query, context)
    elif data == "action_cancel":
        await query.edit_message_text("❌ Watch creation cancelled.")
        return ConversationHandler.END
    elif data == "action_price":
        await query.edit_message_text("💰 Send the maximum price (in numbers only, e.g. `45000`):")
        return SET_PRICE
    elif data == "action_filter":
        return await show_filter_categories(query, context)
    elif data == "action_condition":
        return await show_condition_menu(query, context)

    return MAIN_MENU

async def set_price_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text.isdigit():
        await update.message.reply_text("❌ Invalid number. Please enter a valid price (e.g. 45000):")
        return SET_PRICE
        
    context.user_data['watch_config']['price'] = int(text)
    
    # Send a dummy message to attach the menu which expects a callback query update usually
    # But since we came from text, we send a new message
    msg = await update.message.reply_text("✅ Price set.")
    
    # Hacky way to reuse show_main_menu which expects a query with .edit_message_text
    # We'll just manually call the logic or mock it. Easier to just rewrite menu logic for 'message' context
    # Or just send new menu.
    
    cfg = context.user_data['watch_config']
    price_str = f"{cfg['price']} EUR" if cfg['price'] else "Any"
    opts_str = ", ".join(cfg['options']) if cfg['options'] else "None"
    
    txt = (
        f"⚙️ **Watch Configuration**\n"
        f"• Model: `{cfg['model']}`\n"
        f"• Market: `{cfg['market']}`\n"
        f"• Price Limit: `{price_str}`\n"
        f"• Filters: `{opts_str}`\n"
    )
    keyboard = [
        [InlineKeyboardButton("💰 Set Max Price", callback_data="action_price")],
        [InlineKeyboardButton("🎨 Add Filters (Paint/Wheels...)", callback_data="action_filter")],
        [InlineKeyboardButton("✅ Start Watch", callback_data="action_save")],
        [InlineKeyboardButton("❌ Cancel", callback_data="action_cancel")]
    ]
    await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return MAIN_MENU

async def show_filter_categories(query, context):
    # Show filters based on options
    keyboard = []

    # Get model from context
    model = context.user_data['watch_config'].get('model', 'my')
    
    # Dynamic categories from option_codes.py for this model
    model_opts = OPTION_CODES_DATA.get(model, {})
    
    # Fallback if empty (e.g. invalid model code)
    if not model_opts and 'my' in OPTION_CODES_DATA:
         model_opts = OPTION_CODES_DATA['my']
         
    for cat in model_opts.keys():
        keyboard.append([InlineKeyboardButton(f"📂 {cat}", callback_data=f"cat_{cat}")])

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])

    await query.edit_message_text(f"Select a category ({model}):", reply_markup=InlineKeyboardMarkup(keyboard))
    return SET_OPTION

async def filter_category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_main" or data == "action_filter":
        return await show_main_menu(query, context)

    if data.startswith("cat_"):
        cat = data.split("_")[1]
        # Show options for this cat
        keyboard = []

        model = context.user_data['watch_config'].get('model', 'my')
        model_opts = OPTION_CODES_DATA.get(model, {})
        # Fallback
        if not model_opts and 'my' in OPTION_CODES_DATA: model_opts = OPTION_CODES_DATA['my']

        codes_map = model_opts.get(cat, {})

        for c, name in codes_map.items():
            # Mark if selected
            prefix = "✅ " if c in context.user_data['watch_config']['options'] else ""
            keyboard.append([InlineKeyboardButton(f"{prefix}{name}", callback_data=f"toggle_{c}")])

        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="action_filter")])
        await query.edit_message_text(f"Select {cat}:", reply_markup=InlineKeyboardMarkup(keyboard))
        return SET_OPTION

    if data.startswith("toggle_"):
        code = data.split("_")[1]
        current = context.user_data['watch_config']['options']
        if code in current:
            current.remove(code)
        else:
            current.append(code)

        # Refresh current view (stay in category)
        # We need to find which category this code belongs to
        
        # We need model context
        model = context.user_data['watch_config'].get('model', 'my')
        model_opts = OPTION_CODES_DATA.get(model, {})
        if not model_opts and 'my' in OPTION_CODES_DATA: model_opts = OPTION_CODES_DATA['my']
        
        target_cat = "Other"
        for cat, opts in model_opts.items():
            if code in opts:
                target_cat = cat
                break

        # Re-render list
        keyboard = []
        codes_map = model_opts.get(target_cat, {})
        for c, name in codes_map.items():
            prefix = "✅ " if c in current else ""
            keyboard.append([InlineKeyboardButton(f"{prefix}{name}", callback_data=f"toggle_{c}")])

        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="action_filter")])
        await query.edit_message_text(f"Select {target_cat}:", reply_markup=InlineKeyboardMarkup(keyboard))

        return SET_OPTION

async def save_watch(query, context):
    cfg = context.user_data['watch_config']

    # Save to DB
    db = context.bot_data['db']
    chat_id = str(query.message.chat_id)
    
    # Clean up config for storage
    criteria = {
        'model': cfg['model'],
        'market': cfg['market'],
        'price': cfg['price'],
        'condition': cfg['condition'],
        'options': list(set(cfg['options'])) # unique
    }
    
    # If editing, use existing ID
    watch_id = cfg.get('id')
    
    if watch_id:
        # Update existing
        user = await db.get_user(chat_id)
        if user and 'watches' in user:
            # Replace logic
            new_watches = []
            for w in user['watches']:
                if w['id'] == watch_id:
                    criteria['id'] = watch_id
                    criteria['seen_vins'] = w.get('seen_vins', []) # Keep history
                    
                    # Ensure new fields (condition_mode) are saved
                    criteria['condition'] = cfg.get('condition', 'new')
                    criteria['condition_mode'] = cfg.get('condition_mode', 'all_new')
                    
                    new_watches.append(criteria)
                else:
                    new_watches.append(w)
            await db.update_user(chat_id, {'watches': new_watches})
            action = "Updated"
    else:
        # Create new
        watch_id = db.add_watch(chat_id, criteria)
        action = "Activated"

    start_inventory_job(context.job_queue, chat_id)
    
    await query.edit_message_text(f"✅ **Watch {action}!**\nID: `{watch_id}`\nWe will notify you when a match is found.", parse_mode='Markdown')
    return ConversationHandler.END

# --- New Commands ---

@check_auth
async def inv_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger inventory check"""
    # Trigger job immediately
    await update.message.reply_text("🔎 Triggering immediate check...")
    
    # Manually invoke job logic
    # Reusing job function but we need a mock job object context or just extract logic.
    # Easiest: extract logic to 'run_check_for_user(chat_id)'
    
    chat_id = update.effective_chat.id
    db = context.bot_data['db']
    inv = context.bot_data['inventory']
    
    # Copy paste logic from inventory_job for immediate feedback
    # Or cleaner: refactor inventory_job to call helper.
    # Refactoring inline here:
    
    user = await db.get_user(chat_id)
    watches = user.get('watches', [])
    if not watches:
        await update.message.reply_text("No watches to check.")
        return

    count_found = 0
    for watch in watches:
        results = await inv.check_inventory(watch)
        matches = inv.find_matches(results, watch)
        
        seen_vins = set(watch.get('seen_vins', []))
        new_matches = [m for m in matches if m.get('VIN') not in seen_vins]
        
        if new_matches:
            count_found += len(new_matches)
            for car in new_matches:
                msg = inv.format_car(car)
                await update.message.reply_text(msg, parse_mode='Markdown')
                seen_vins.add(car.get('VIN'))
            
            # Update seen vins (non-atomic but fine for manual trigger)
            watch['seen_vins'] = list(seen_vins)
            
    await db.update_user(chat_id, {'watches': watches})
    await update.message.reply_text(f"✅ Check complete. Found {count_found} new matches.")

@check_auth
async def inv_edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit an existing watch: /inv_edit <id>"""
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/inv_edit <watch_id>`")
        return
        
    watch_id = args[0]
    chat_id = update.effective_chat.id
    db: UserDatabase = context.bot_data['db']
    user = await db.get_user(chat_id)
    
    target_watch = None
    if user and 'watches' in user:
        for w in user['watches']:
            if w['id'] == watch_id:
                target_watch = w
                break
                
    if not target_watch:
        await update.message.reply_text("❌ Watch ID not found.")
        return
        
    # Populate context
    context.user_data['watch_config'] = {
        'id': watch_id,
        'model': target_watch.get('model', 'my'),
        'market': target_watch.get('market', 'ES'),
        'condition': target_watch.get('condition', 'new'),
        'options': target_watch.get('options', []),
        'price': target_watch.get('price')
    }
    
    # Start Wizard at Menu
    # We need to send a message because wizard expects callback queries usually,
    # but show_main_menu uses edit_message_text on query.
    # Only set_price_handler handles message update.
    # So we manually send the message with same markup as show_main_menu
    
    cfg = context.user_data['watch_config']
    price_str = f"{cfg['price']} EUR" if cfg['price'] else "Any"
    opts_str = ", ".join(cfg['options']) if cfg['options'] else "None"
    
    text = (
        f"⚙️ **Editing Watch: {watch_id}**\n"
        f"• Model: `{cfg['model']}`\n"
        f"• Market: `{cfg['market']}`\n"
        f"• Price Limit: `{price_str}`\n"
        f"• Filters: `{opts_str}`\n"
    )
    
    keyboard = [
        [InlineKeyboardButton("💰 Set Max Price", callback_data="action_price")],
        [InlineKeyboardButton("🎨 Add Filters (Paint/Wheels...)", callback_data="action_filter")],
        [InlineKeyboardButton("✅ Save Changes", callback_data="action_save")],
        [InlineKeyboardButton("❌ Cancel", callback_data="action_cancel")]
    ]
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return MAIN_MENU

async def cancel_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Wizard cancelled.")
    return ConversationHandler.END

# Legacy wrapper
async def legacy_inv_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /inv_watch model=my market=ES price=50000 condition=new
    """
    args = context.args
    criteria = {}
    try:
        for arg in args:
            key, val = arg.split('=')
            if key == 'price': val = int(val)
            if key == 'options': val = val.split(',')
            criteria[key] = val
            
        # Add to DB
        db = context.bot_data['db']
        chat_id = update.effective_chat.id
        watch_id = db.add_watch(chat_id, criteria)
        
        start_inventory_job(context.job_queue, chat_id)
        
        await update.message.reply_text(f"✅ Watch added! ID: `{watch_id}`\nCriteria: {criteria}", parse_mode='Markdown')
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error parsing arguments. Use `key=value`. ({e})")


@check_auth
async def inv_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: UserDatabase = context.bot_data['db']
    chat_id = update.effective_chat.id
    user = await db.get_user(chat_id)
    watches = user.get('watches', [])
    
    if not watches:
        await update.message.reply_text("You have no active watches.")
        return
        
    msg = "**👀 Active Watches:**\n"
    for w in watches:
        msg += f"🆔 `{w['id']}`: {w.get('model','my')} in {w.get('market','ES')} < {w.get('price','No Limit')}\n"
    
    msg += "\nTo remove: `/inv_del <id>`"
    await update.message.reply_text(msg, parse_mode='Markdown')

@check_auth
async def inv_del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/inv_del <watch_id>`")
        return
        
    watch_id = args[0]
    db: UserDatabase = context.bot_data['db']
    success = db.remove_watch(update.effective_chat.id, watch_id)
    
    if success:
        await update.message.reply_text("🗑️ Watch deleted.")
    else:
        await update.message.reply_text("❌ Watch ID not found.")

# --- Background Inventory Job ---

async def inventory_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    db: UserDatabase = context.bot_data['db']
    inv: InventoryManager = context.bot_data['inventory']
    
    user = await db.get_user(chat_id)
    watches = user.get('watches', [])
    
    if not watches:
        return # No watches, do nothing

    for watch in watches:
        # 1. Fetch
        results = await inv.check_inventory(watch)
        
        # 2. Filter
        matches = inv.find_matches(results, watch)
        
        # 3. Notify (Simple diff: we don't store seen VINs per watch yet, so this might spam if not careful)
        # To avoid spam, we'll only notify if it's "fresh" (not in a local temp cache for this run? No, need persistent memory of seen VINs).
        # IMPLEMENTATION SHORTCUT: For now, just send top 1 match if not seen before?
        # Better: Store 'seen_vins' in the watch object in DB.
        
        seen_vins = set(watch.get('seen_vins', []))
        new_matches = [m for m in matches if m.get('VIN') not in seen_vins]
        
        if new_matches:
            for car in new_matches: # Limit to 3 notifications
                msg = inv.format_car(car)
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
                seen_vins.add(car.get('VIN'))
            
            # Update seen vins
            watch['seen_vins'] = list(seen_vins)
            # This save needs to be done on the user object, not directly on db.save()
            # The watch object is a copy from the user's watches list.
            # We need to update the user's watches list in the db.
            await db.update_user(chat_id, {'watches': watches}) # Update the entire watches list for the user

def start_inventory_job(queue, chat_id):
    # Check if job exists
    name = f"inv_{chat_id}"
    current_jobs = queue.get_jobs_by_name(name)
    if not current_jobs:
        # Run every 10 minutes (600s)
        queue.run_repeating(inventory_job, interval=600, first=10, chat_id=chat_id, name=name)

# --- Main Status & Diffing Logic ---

async def check_orders_task(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    db = context.bot_data['db']
    client = TeslaClient(chat_id, db)
    
    try:
        user = await db.get_user(chat_id)
        if not user: return # Should not happen

        prev_map = user.get('orders_state', {})
        curr_map = {}
        
        orders = await client.get_orders()
        for order in orders:
            rn = order['referenceNumber']
            details = await client.get_order_details(rn)
            
            curr_map[rn] = {'summary': order, 'details': details}
            
            # Diff
            old_data = prev_map.get(rn)
            notify = False
            
            if not old_data:
                notify = True # New
            else:
                if old_data['summary'].get('vin') != order.get('vin'): notify = True
                if old_data['details']['tasks']['scheduling'].get('deliveryWindowDisplay') != details['tasks']['scheduling'].get('deliveryWindowDisplay'): notify = True
                
            if notify:
                msg, url = format_full_message(order, details)
                try:
                    await context.bot.send_photo(chat_id, url, caption=msg, parse_mode='Markdown')
                except:
                    await context.bot.send_message(chat_id, msg, parse_mode='Markdown')
        
        # Save state
        await db.update_user(chat_id, {'orders_state': curr_map})
        
    except Exception as e:
        logger.error(f"Job failed for {chat_id}: {e}")

@check_auth
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = context.bot_data['db']
    client = TeslaClient(chat_id, db)
    
    await update.message.reply_text("🔄 Checking...")
    try:
        orders = await client.get_orders()
        for order in orders:
            details = await client.get_order_details(order['referenceNumber'])
            msg, url = format_full_message(order, details)
            try:
                await update.message.reply_photo(url, caption=msg, parse_mode='Markdown')
            except:
                await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

def format_full_message(order, details):
    rn = order['referenceNumber']
    status = order.get('orderStatus', 'Unknown')
    model = order.get('modelCode', 'Unknown')
    vin = order.get('vin')
    
    # Extract details
    tasks = details.get('tasks', {})
    sched = tasks.get('scheduling', {})
    reg = tasks.get('registration', {})
    reg_details = reg.get('orderDetails', {})
    final_payment = tasks.get('finalPayment', {}).get('data', {})
    
    # Fields
    reservation_date = reg_details.get('reservationDate', 'N/A')
    delivery_window = sched.get('deliveryWindowDisplay', 'Pending')
    routing_loc = reg_details.get('vehicleRoutingLocation', 'N/A')
    eta_to_center = final_payment.get('etaToDeliveryCenter', 'N/A')
    appt = sched.get('apptDateTimeAddressStr', 'Not Scheduled')
    
    # Build Message
    msg = (
        f"🚗 **Tesla Order: {rn}**\n"
        f"**Status:** {status}\n"
        f"**Model:** {model}\n\n"
    )
    
    if vin:
        msg += f"✅ **VIN Assigned:** `{vin}`\n🏭 {decode_vin(vin)}\n"
    else:
        msg += "⛔ **VIN:** Not Assigned Yet\n"
        
    msg += (
        f"\n📍 **Logistics**\n"
        f"• **Location:** {routing_loc}\n"
        f"• **ETA to Center:** {eta_to_center}\n"
        f"• **Appointment:** {appt}\n"
    )

    msg += (
        f"\n📅 **Dates**\n"
        f"• **Reserved:** {reservation_date}\n"
        f"• **Window:** {delivery_window}\n"
    )
    
    # Blocking steps
    steps = reg.get('tasks', [])
    blocking = [s['name'] for s in steps if not s['complete'] and s['status'] != 'COMPLETE']
    if blocking:
        msg += "\n⚠️ **Action Required:**\n" + "\n".join([f"• {b}" for b in blocking[:3]])
        
    return msg, get_image_url(order.get('optionCodeList', []), model)

# --- Infrastructure ---

def start_job(job_queue, chat_id, interval_seconds):
    # Remove existing
    jobs = job_queue.get_jobs_by_name(str(chat_id))
    for j in jobs: j.schedule_removal()
    
    job_queue.run_repeating(check_orders_task, interval=interval_seconds, first=10, chat_id=chat_id, name=str(chat_id))

async def post_init(application):
    db = application.bot_data['db']
    asyncio.create_task(health_check_server())
    
    # Restore jobs
    users = await db.get_all_users()
    logger.info(f"Restoring jobs for {len(users)} users...")
    
    for uid in users:
        u_data = await db.get_user(uid)
        interval = u_data.get('interval', 30) * 60
        start_job(application.job_queue, uid, interval)

        # Restore inventory jobs
        if u_data.get('watches'):
            start_inventory_job(application.job_queue, uid)


async def health_check_server():
    async def handle(r): return web.Response(text="OK")
    app = web.Application()
    app.add_routes([web.get('/health', handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()

if __name__ == '__main__':
    # Initialize
    db = UserDatabase()
    inventory_manager = InventoryManager(db)
    
    app = ApplicationBuilder().token(os.getenv('TELEGRAM_TOKEN')).post_init(post_init).build()
    app.bot_data['db'] = db
    app.bot_data['inventory'] = inventory_manager
    
    # Handlers
    app.add_handler(CommandHandler('start', help_command))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('login', login_command))
    app.add_handler(CommandHandler('logout', logout_command))
    app.add_handler(CommandHandler('interval', interval_command))
    app.add_handler(CommandHandler('status', status_command))
    app.add_handler(CommandHandler('vin', vin_command))
    app.add_handler(CommandHandler('options', options_command))
    app.add_handler(CommandHandler('image', image_command))
    
    # Inventory handlers
    app.add_handler(CommandHandler('inv_test', inv_test_command))
    # Wizard Conversation Handler
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('inv_watch', start_watch_wizard),
            CommandHandler('inv_edit', inv_edit_command)
        ],
        states={
            SELECT_MODEL: [CallbackQueryHandler(select_model)],
            SELECT_MARKET: [CallbackQueryHandler(select_market)],
            MAIN_MENU: [CallbackQueryHandler(menu_handler)],
            SET_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_price_handler)],
            SELECT_CONDITION: [CallbackQueryHandler(select_condition)],
            SET_OPTION: [CallbackQueryHandler(filter_category_handler)]
        },
        fallbacks=[CommandHandler('cancel', cancel_wizard)]
    )
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler('inv_list', inv_list_command))
    app.add_handler(CommandHandler('inv_del', inv_del_command))
    app.add_handler(CommandHandler('inv_check', inv_check_command))
    
    # Register ConversationHandler for /inv_edit too?
    # No, ConversationHandler entry_points list can handle multiple commands!
    # Updating conv_handler definition below via next edit or re-definition.
    # Actually, we can just add another entry point to the existing conv_handler logic if we modify it above.
    # But since we are patching, let's just make a new one or Modify the existing one.
    # Easier: Just replace the definition of conv_handler

    
    # On startup, restart jobs for existing users with watches
    # (Simplified: users need to trigger /inv_watch to start job, or we iterate all users here)
    # Ideally: iterate all users and start jobs.
    # loop = asyncio.get_event_loop() # Get loop to run async DB call synchronously-ish or just assume safe
    # Skipping auto-restart of inventory jobs for now to keep it simple, user just adds watch or we add a "startup" logic.
    # Actually, let's just let them run /inv_watch to ensure job is running.
    
    # Error & Unknown
    app.add_error_handler(error_handler)
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    
    logger.info("Bot is polling...")
    app.run_polling()