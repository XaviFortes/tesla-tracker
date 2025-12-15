import asyncio
import json
from inventory import InventoryManager

# Mock DB
class MockDB: pass

async def debug_inv():
    # Helper to check ES market for Model Y
    criteria = {
        'market': 'ES',
        'model': 'my',
        'condition': 'new',
        'zip': '28001' # Madrid
    }
    
    inv = InventoryManager(MockDB())
    print("Fetching inventory...")
    results = await inv.check_inventory(criteria)
    print(f"Found {len(results)} cars.")
    
    found_mty62 = False
    
    for i, car in enumerate(results):
        opts = car.get('OptionCodeList', [])
        vin = car.get('VIN')
        
        # Check if MTY62 is present
        has_mty62 = any('MTY62' in o for o in opts)
        
        if has_mty62:
            found_mty62 = True
            print(f"\nExample MTY62 Car ({vin}):")
            print(f"Options: {opts}")
            
            # Test match logic
            test_criteria = {'options': ['$MTY62']}
            matches = inv.find_matches([car], test_criteria)
            if matches:
                 print(" -> MATCHED with logic")
            else:
                 print(" -> FAILED match logic")
                 
    if not found_mty62:
        print("\nNo MTY62 cars found in current inventory sample.")
        # Print first car options just to see
        if results:
            print(f"First Car Options: {results[0].get('OptionCodeList')}")

if __name__ == "__main__":
    asyncio.run(debug_inv())
