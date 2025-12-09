import os
import json
import asyncio
import logging
import httpx
from aiohttp import web
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# --- Configuration ---
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
DB_FILE = '/data/tesla_users_v2.json'  # New DB file for multi-user
CLIENT_ID = 'ownerapi'
TOKEN_URL = 'https://auth.tesla.com/oauth2/v3/token'
APP_VERSION = '4.35.1-2716' # Updated slightly to be safe

# --- Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=LOG_LEVEL
)
logger = logging.getLogger(__name__)

# --- Decoders & Data (Re-used) ---
OPTION_CODES = {
    "AP04": "Autopilot HW 4.0", "APH4": "Autopilot HW 3.0", "APF0": "FSD Capability",
    "BT37": "Battery: 75kWh (Panasonic)", "BT43": "Battery: 78kWh (LG)", "BTF1": "Battery: LFP (CATL)",
    "PPSW": "White Paint", "PPSB": "Blue Paint", "PMNG": "Midnight Silver", 
    "PRMQ": "Red Multi-Coat", "PBSB": "Black Paint", "PPMR": "Red Multi-Coat",
    "W40B": "19'' Gemini Wheels", "W41B": "20'' Induction Wheels", "W38B": "18'' Aero Wheels",
    "IB00": "Black Interior", "IB01": "White Interior",
}
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

# --- Commands ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 **Tesla Tracker Commands**\n\n"
        "🔐 **Setup**\n"
        "`/login <refresh_token>` - Authorize bot (Revokes old tokens!)\n"
        "`/logout` - Remove your data\n"
        "`/interval <minutes>` - Set check frequency (default 30m)\n\n"
        "🚘 **Inspect**\n"
        "`/status` - Full report of all orders\n"
        "`/vin` - Only VIN & Factory info\n"
        "`/options` - Decoded configuration code\n"
        "`/image` - Get vehicle render\n\n"
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
    
    # Notify user we are validating
    status_msg = await update.message.reply_text("🔐 Validating token with Tesla...")
    
    try:
        # Save momentarily to test
        await db.update_user(chat_id, {'refresh_token': refresh_token, 'access_token': None, 'interval': 30})
        
        # Test connection
        client = TeslaClient(chat_id, db)
        orders = await client.get_orders() # This triggers refresh
        
        await status_msg.edit_text(f"✅ Success! Found {len(orders)} orders.\nPolling started (30m interval).")
        
        # Start Job
        start_job(context.job_queue, chat_id, 30*60)
        
        # Security: Try to delete the message containing the token
        try:
            await update.message.delete()
        except:
            pass # Bot might not have delete permissions
            
    except Exception as e:
        await db.delete_user(chat_id) # Cleanup bad data
        await status_msg.edit_text(f"❌ Login Failed: {str(e)}")

async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db = context.bot_data['db']
    
    await db.delete_user(chat_id)
    
    # Remove jobs
    jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in jobs:
        job.schedule_removal()
        
    await update.message.reply_text("👋 Logged out. Data removed.")

async def interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
    db = context.bot_data['db']
    
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: `/interval 60` (in minutes)", parse_mode='Markdown')
        return
        
    minutes = int(args[0])
    if minutes < 5:
        await update.message.reply_text("❌ Minimum interval is 5 minutes.")
        return
        
    await db.update_user(chat_id, {'interval': minutes})
    
    # Reschedule
    start_job(context.job_queue, chat_id, minutes*60)
    await update.message.reply_text(f"✅ Polling interval set to {minutes} minutes.")

# --- Specific Feature Commands ---

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
    vin = order.get('vin')
    window = details['tasks']['scheduling'].get('deliveryWindowDisplay', 'Pending')
    
    msg = f"🚗 **Update: {rn}**\n**Delivery:** {window}\n"
    if vin: msg += f"**VIN:** `{vin}`\nBased in: {decode_vin(vin)}\n"
    
    # Blocking steps
    steps = details.get('tasks', {}).get('registration', {}).get('tasks', [])
    blocking = [s['name'] for s in steps if not s['complete'] and s['status'] != 'COMPLETE']
    if blocking:
        msg += "\n⚠️ **Action Required:**\n" + "\n".join([f"• {b}" for b in blocking[:3]])
        
    return msg, get_image_url(order.get('optionCodeList', []), order.get('modelCode', 'my'))

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

async def health_check_server():
    async def handle(r): return web.Response(text="OK")
    app = web.Application()
    app.add_routes([web.get('/health', handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    await site.start()

if __name__ == '__main__':
    db = UserDatabase()
    
    app = ApplicationBuilder().token(os.getenv('TELEGRAM_TOKEN')).post_init(post_init).build()
    app.bot_data['db'] = db
    
    app.add_handler(CommandHandler('start', help_command))
    app.add_handler(CommandHandler('help', help_command))
    app.add_handler(CommandHandler('login', login_command))
    app.add_handler(CommandHandler('logout', logout_command))
    app.add_handler(CommandHandler('interval', interval_command))
    app.add_handler(CommandHandler('status', status_command))
    app.add_handler(CommandHandler('vin', vin_command))
    app.add_handler(CommandHandler('options', options_command))
    app.add_handler(CommandHandler('image', image_command))
    
    app.run_polling()