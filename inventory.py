import os
import logging
import json
from curl_cffi.requests import AsyncSession 
import asyncio
from datetime import datetime

logger = logging.getLogger(__name__)

class InventoryManager:
    def __init__(self, db):
        self.db = db
        # Cache results to avoid spamming Tesla: { "ES_my_new_Price": { "timestamp": ..., "results": [...] } }
        self.cache = {} 
        self.cache_ttl = 300  # 5 minutes

    async def check_inventory(self, criteria):
        """
        Query Tesla Inventory API v4.
        """
        market = criteria.get('market', 'ES')
        model = criteria.get('model', 'my')
        condition = criteria.get('condition', 'new')
        # Default to Madrid if not specified
        lat = criteria.get('lat', 40.4168)
        lng = criteria.get('lng', -3.7038)
        zip_code = criteria.get('zip', '28001')
        
        # Build options filters (e.g. TRIM)
        query_options = {}
        if 'trim' in criteria:
            query_options['TRIM'] = [criteria['trim']]
        
        cache_key = f"{market}_{model}_{condition}_{criteria.get('trim','all')}"
        
        # Check cache
        if cache_key in self.cache:
            entry = self.cache[cache_key]
            if (datetime.now().timestamp() - entry['timestamp']) < self.cache_ttl:
                logger.info(f"Using cached inventory for {cache_key}")
                return entry['results']

        url = "https://www.tesla.com/inventory/api/v4/inventory-results"
        
        # Structure derived from user input
        query_payload = {
            "query": {
                "model": model,
                "condition": condition,
                "options": query_options,
                "arrangeby": "Price",
                "order": "asc",
                "market": market,
                "language": "es" if market == 'ES' else "en",
                "super_region": "europe" if market in ['ES', 'FR', 'DE', 'IT', 'NL', 'NO', 'SE'] else "north america",
                "lng": lng,
                "lat": lat,
                "zip": zip_code,
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
        
        params = {"query": json.dumps(query_payload)}
        
        # Construct Referer exactly like the browser/test script
        locale = "es_ES" if market == 'ES' else f"{market.lower()}_{market}"
        referer = f"https://www.tesla.com/{locale}/inventory/{condition}/{model}?arrangeby=plh&zip={zip_code}&range=0"
        
        # Headers for curl_cffi 
        # We ONLY provide context-specific headers. 
        # Browser identifiers (User-Agent, Sec-CH-UA, Priority, etc.) are handled by impersonate="chrome120"
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "referer": referer,
            "origin": "https://www.tesla.com"
        }
        
        try:
            # Check for proxy
            proxy = os.getenv('INVENTORY_PROXY')
            if proxy:
                logger.info(f"Using proxy: {proxy.split('@')[-1]}") 

            # Use curl_cffi to impersonate Chrome 120
            async with AsyncSession(impersonate="chrome120", timeout=20.0, proxy=proxy) as client:
                resp = await client.get(url, params=params, headers=headers)
                
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get('results', [])
                    # Update cache
                    self.cache[cache_key] = {
                        "timestamp": datetime.now().timestamp(),
                        "results": results
                    }
                    return results
                else:
                    logger.error(f"Inventory API Error {resp.status_code}")
                    logger.error(f"Req URL: {url}")
                    logger.error(f"Resp Body: {resp.text}")
                    return []
        except Exception as e:
            logger.error(f"Inventory Request Failed: {e}")
            return []

    def find_matches(self, results, criteria):
        """
        Filter generic results against specific user criteria (max_price, specific option codes).
        """
        matches = []
        max_price = criteria.get('price')
        required_options = criteria.get('options', []) # List of option codes e.g. ['W40B', 'PPSW']
        
        for car in results:
            # Price Check
            price = car.get('OnTheRoadPrice', car.get('Price', 999999))
            if max_price and price > max_price:
                continue
                
            # Option Check
            car_options = car.get('OptionCodeMap', {}).keys()
            # If we don't have map, parsing options list might be needed, but simplified for now:
            if not car_options:
                car_options = car.get('OptionCodeList', [])
                
            if required_options:
                # Check if ALL required options are present
                if not all(opt in car_options for opt in required_options):
                    continue
            
            matches.append(car)
            
        return matches

    def format_car(self, car):
        vin = car.get('VIN', 'N/A')
        price = car.get('OnTheRoadPrice', car.get('Price', 'N/A'))
        currency = car.get('CurrencyCode', 'EUR')
        trim = car.get('TrimName', 'Unknown Trim')
        color = car.get('PaintColor', 'Unknown Color')
        city = car.get('City', 'Unknown Location')
        
        msg = (
            f"🚙 **Inventory Found!**\n"
            f"**Price:** {price} {currency}\n"
            f"**Trim:** {trim}\n"
            f"**Color:** {color}\n"
            f"**City:** {city}\n"
            f"🔗 [View Car](https://www.tesla.com/{car.get('Market','ES')}/order/{vin}?#aux-1-content)"
        )
        return msg
