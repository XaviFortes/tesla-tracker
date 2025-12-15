from inventory import InventoryManager

# Mock DB
class MockDB: pass

def test_filtering():
    inv = InventoryManager(MockDB())
    
    # Mock Cars
    car_lr_1 = {"OptionCodeList": ["$MTY41", "$PPSW", "$WY19P", "$CPF0"], "VIN": "1"}
    car_lr_2 = {"OptionCodeList": ["$MTY47", "$PBSB", "$WY20A", "$CPF0"], "VIN": "2"}
    car_rwd = {"OptionCodeList": ["$MTY52", "$PPSW", "$WY19P", "$CPF0"], "VIN": "3"}
    
    results = [car_lr_1, car_lr_2, car_rwd]
    
    print("--- Test 1: OR Logic for Trims ---")
    # User wants EITHER MTY41 OR MTY47
    criteria = {"options": ["$MTY41", "$MTY47"]} 
    matches = inv.find_matches(results, criteria)
    print(f"Matches (Exp 2 cars): {len(matches)}")
    for m in matches: print(f" - Found: {m['OptionCodeList']}")
    assert len(matches) == 2
    
    print("\n--- Test 2: Mixed OR (Paint) and AND (Trim) ---")
    # User wants MTY41 (AND) + (Red OR Blue) -> here (White OR Black) using actual codes
    # MTY41 + (PPSW=White OR PBSB=Black)
    criteria = {"options": ["$MTY41", "$PPSW", "$PBSB"]}
    matches = inv.find_matches(results, criteria)
    print(f"Matches (Exp 1 car - car_lr_1): {len(matches)}")
    for m in matches: print(f" - Found: {m['OptionCodeList']}")
    assert len(matches) == 1
    assert matches[0]['VIN'] == "1"
    
    print("\n--- Test 3: Standard AND (Other Option) ---")
    # MTY41 + CPF0
    criteria = {"options": ["$MTY41", "$CPF0"]}
    matches = inv.find_matches(results, criteria)
    print(f"Matches (Exp 1 car): {len(matches)}")
    assert len(matches) == 1
    
    print("\n--- Test 4: Fail Case (Wrong Trim) ---")
    # MTY52 (RWD) + CPF0
    criteria = {"options": ["$MTY52", "$CPF0"]}
    matches = inv.find_matches(results, criteria)
    # Should get car 3
    print(f"Matches (Exp 1 car): {len(matches)}")
    assert len(matches) == 1
    
    print("\n--- Test 5: Fail Case (Missing Other Option) ---")
    # MTY41 + NON_EXISTENT
    criteria = {"options": ["$MTY41", "$NON_EXISTENT"]}
    matches = inv.find_matches(results, criteria)
    print(f"Matches (Exp 0): {len(matches)}")
    assert len(matches) == 0

    print("\n✅ All Tests Passed")

if __name__ == "__main__":
    test_filtering()
