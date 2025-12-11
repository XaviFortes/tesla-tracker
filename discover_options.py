import asyncio
import httpx
import json
import logging
import os
import sys

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
MODELS = ['my', 'm3', 'ms', 'mx']
MARKET = "ES"

async def fetch_inventory(model, market="ES"):
    url = "https://www.tesla.com/inventory/api/v4/inventory-results"
    
    query = {
        "query": {
            "model": model,
            "condition": "new",
            "options": {},
            "arrangeby": "Price",
            "order": "asc",
            "market": market,
            "language": "es" if market == "ES" else "en",
            "super_region": "europe" if market in ["ES", "FR", "DE"] else "north america",
            "lng": -3.7038,
            "lat": 40.4168,
            "zip": "28001",
            "range": 0,
            "region": market
        },
        "offset": 0,
        "count": 50,
        "outsideOffset": 0,
        "outsideSearch": False,
        "isFalconDeliverySelectionEnabled": True,
        "version": "v2"
    }

    headers = {
        "authority": "www.tesla.com",
        "method": "GET",
        "scheme": "https",
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "priority": "u=1, i",
        "referer": f"https://www.tesla.com/{market.lower()}_{market}/inventory/new/{model}",
        "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "origin": "https://www.tesla.com"
    }

    async with httpx.AsyncClient(timeout=10.0, http2=True) as client:
        try:
            logger.info(f"Fetching {model} in {market}...")
            resp = await client.get(url, params={"query": json.dumps(query)}, headers=headers)
            if resp.status_code != 200:
                logger.error(f"Error {resp.status_code}: {resp.text[:200]}")
                return None
            return resp.json()
        except Exception as e:
            logger.error(f"Request failed: {e}")
            return None

def extract_options(data):
    if not data: return {}
    
    extracted = {}
    results = data.get("results", [])
    logger.info(f"Scanning {len(results)} cars...")
    
    for car in results:
        options_data = car.get("OptionCodeData", [])
        iterator = options_data if isinstance(options_data, list) else options_data.values()
        
        for opt in iterator:
            code = opt.get("code")
            name = opt.get("name")
            group = opt.get("group", "OTHER")
            if not group: group = opt.get("lexiconGroup", "OTHER")

            if not code or not name: continue
            
            # Normalize group name
            if group in ["PAINT", "Paint", "PAINT_COLOR"]: group = "Paint"
            elif group in ["WHEELS", "Wheels", "WHEEL_TYPE"]: group = "Wheels"
            elif group in ["INTERIOR", "Interior", "Reats_Seats", "REAR_SEATS", "INTERIOR_PACKAGE"]: group = "Interior"
            elif group in ["AUTOPILOT", "Autopilot", "AUTOPILOT_PACKAGE"]: group = "Autopilot"
            else: group = "Other"
            
            if group not in extracted: extracted[group] = {}
            extracted[group][code] = name
            
    return extracted

def load_existing_options():
    try:
        if os.path.exists("option_codes.py"):
            with open("option_codes.py", "r") as f:
                content = f.read()
            
            scope = {}
            exec(content, scope)
            data = scope.get("OPTION_CODES_DATA", {})
            # Verify structure: if flat (old format), migrate it or just return specific format
            # Old format: {'Paint': {...}, ...} (root is categories)
            # New format: {'my': {'Paint': ...}, ...} (root is models)
            
            # Heuristic: Check if keys are models
            if any(k in data for k in MODELS):
                 return data
            else:
                # Assume it's legacy 'generic' data, assign to 'all' or merge into all models?
                # Better: Drop legacy or try to auto-assign. 
                # Let's start fresh or just return empty if format mismatch to avoid corruption.
                logger.warning("Detected legacy option_codes format. Starting fresh structure.")
                return {}
                
    except Exception as e:
        logger.error(f"Failed to load existing options from file: {e}")
    return {}

def merge_options_for_model(existing_root, model, new_model_data):
    # existing_root is {'my': {...}, 'm3': {...}}
    
    if model not in existing_root:
        existing_root[model] = {}
        
    for cat, items in new_model_data.items():
        if cat not in existing_root[model]:
            existing_root[model][cat] = {}
        existing_root[model][cat].update(items)
        
    return existing_root

async def main():
    final_options = load_existing_options()
    
    for model in MODELS:
        data = await fetch_inventory(model, MARKET)
        if data:
            new_opts = extract_options(data)
            logger.info(f"Found {sum(len(v) for v in new_opts.values())} options for {model}.")
            final_options = merge_options_for_model(final_options, model, new_opts)
            await asyncio.sleep(1)

    # Sort
    sorted_root = {}
    for model, cats in final_options.items():
        sorted_root[model] = {k: dict(sorted(v.items())) for k, v in sorted(cats.items())}

    with open("option_codes.py", "w") as f:
        f.write("# Auto-generated Option Codes\n# Structure: Model -> Category -> Code: Name\n\n")
        f.write("OPTION_CODES_DATA = ")
        json.dump(sorted_root, f, indent=2, ensure_ascii=False)
        f.write("\n")
    logger.info("Saved to option_codes.py")

if __name__ == "__main__":
    asyncio.run(main())
