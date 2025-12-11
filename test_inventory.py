import httpx
import json
import asyncio

async def test_inventory():
    # TEST CONFIG
    market = 'ES'
    model = 'my'
    condition = 'new'
    trim = 'LRAWD' # Test specific trim
    lat = 40.4168
    lng = -3.7038
    zip_code = '28001'

    url = "https://www.tesla.com/inventory/api/v4/inventory-results"
    
    # Build options exactly like InventoryManager
    query_options = {}
    if trim:
        query_options['TRIM'] = [trim]

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
    
    params = {
        "query": json.dumps(query_payload)
    }
    
    headers = {
        "authority": "www.tesla.com",
        "method": "GET",
        "scheme": "https",
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "priority": "u=1, i",
        "referer": "https://www.tesla.com/es_ES/inventory/new/my?TRIM=LRAWD&arrangeby=plh&zip=28522&range=0",
        "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "origin": "https://www.tesla.com"
    }

    # Optional: If you want to test with cookies, uncomment this and paste them
    # headers['cookie'] = "YOUR_COOKIES_HERE"

    print(f"--- Request ---")
    print(f"URL: {url}")
    print(f"Params: {json.dumps(params, indent=2)}")
    print(f"Headers: {json.dumps(headers, indent=2)}")

    # Use HTTP/2 to mimic browser
    async with httpx.AsyncClient(timeout=20.0, http2=True) as client:
        resp = await client.get(url, params=params, headers=headers)
        print(f"--- Response ---")
        print(f"Status: {resp.status_code}")
        
        if resp.status_code == 200:
            data = resp.json()
            total = data.get('total_matches_found')
            print(f"Total Matches: {total}")
            results = data.get('results', [])
            print(f"Fetched: {len(results)} cars")
            if results:
                print("--- Sample Car ---")
                car = results[0]
                print(f"VIN: {car.get('VIN')}")
                print(f"Price: {car.get('Price')}")
                print(f"Trim: {car.get('TrimName')}")
                print(f"Options: {car.get('OptionCodeList')}")
        else:
            print("Error Body:")
            print(resp.text[:500])

if __name__ == "__main__":
    asyncio.run(test_inventory())
