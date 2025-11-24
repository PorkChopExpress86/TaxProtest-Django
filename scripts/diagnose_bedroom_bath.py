#!/usr/bin/env python
"""
Diagnostic script to investigate bedroom/bathroom data for account 1074380000028.
Run this with: docker compose exec web python scripts/diagnose_bedroom_bath.py
"""
import os
import sys
import django

# Setup Django
sys.path.insert(0, '/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from data.models import PropertyRecord, BuildingDetail, ExtraFeature


def main():
    account = "1074380000028"
    
    print("=" * 80)
    print(f"DIAGNOSTIC REPORT: Account {account} (16213 Wall St)")
    print("=" * 80)
    
    # 1. Check PropertyRecord
    print("\n1. PROPERTY RECORD")
    print("-" * 80)
    prop = PropertyRecord.objects.filter(account_number=account).first()
    if prop:
        print(f"✓ PropertyRecord found:")
        print(f"  Address: {prop.address}")
        print(f"  Street Name: {prop.street_name}")
        print(f"  Street Number: {prop.street_number}")
        print(f"  City: {prop.city}")
        print(f"  Zipcode: {prop.zipcode}")
        print(f"  Owner: {prop.owner_name}")
        print(f"  Building Area: {prop.building_area}")
        print(f"  Land Area: {prop.land_area}")
        print(f"  Assessed Value: {prop.assessed_value}")
    else:
        print(f"✗ PropertyRecord NOT FOUND for account {account}")
        return
    
    # 2. Check BuildingDetail
    print("\n2. BUILDING DETAILS")
    print("-" * 80)
    buildings = BuildingDetail.objects.filter(account_number=account, is_active=True)
    print(f"Active BuildingDetail records: {buildings.count()}")
    
    if buildings.count() > 0:
        for bd in buildings:
            print(f"\n  Building #{bd.building_number}:")
            print(f"    Type: {bd.building_type}")
            print(f"    Style: {bd.building_style}")
            print(f"    Class: {bd.building_class}")
            print(f"    Year Built: {bd.year_built}")
            print(f"    Heat Area: {bd.heat_area}")
            print(f"    Stories: {bd.stories}")
            print(f"    ")
            print(f"    >>> BEDROOMS: {bd.bedrooms}")
            print(f"    >>> BATHROOMS: {bd.bathrooms}")
            print(f"    >>> HALF BATHS: {bd.half_baths}")
            print(f"    ")
            print(f"    Fireplaces: {bd.fireplaces}")
            print(f"    Import Date: {bd.import_date}")
            print(f"    Import Batch: {bd.import_batch_id}")
    else:
        print("  ✗ No active BuildingDetail records found")
        
        # Check for inactive records
        inactive = BuildingDetail.objects.filter(account_number=account, is_active=False)
        if inactive.count() > 0:
            print(f"  ⚠ Found {inactive.count()} INACTIVE BuildingDetail records")
    
    # 3. Check ExtraFeature records
    print("\n3. EXTRA FEATURES")
    print("-" * 80)
    features = ExtraFeature.objects.filter(account_number=account, is_active=True)
    print(f"Active ExtraFeature records: {features.count()}")
    
    if features.count() > 0:
        # Group by feature code
        from collections import defaultdict
        by_code = defaultdict(list)
        for f in features:
            by_code[f.feature_code].append(f)
        
        for code, feats in sorted(by_code.items()):
            print(f"\n  {code}: {len(feats)} record(s)")
            for f in feats[:3]:  # Show first 3
                print(f"    #{f.feature_number}: {f.feature_description}")
                print(f"      Quantity: {f.quantity}, Area: {f.area}")
    else:
        print("  ✗ No active ExtraFeature records found")
        
        # Check for inactive records
        inactive = ExtraFeature.objects.filter(account_number=account, is_active=False)
        if inactive.count() > 0:
            print(f"  ⚠ Found {inactive.count()} INACTIVE ExtraFeature records")
    
    # 4. Check specifically for room codes
    print("\n4. ROOM DATA (RMB, RMF, RMH)")
    print("-" * 80)
    room_codes = ['RMB', 'RMF', 'RMH']
    room_features = ExtraFeature.objects.filter(
        account_number=account,
        feature_code__in=room_codes,
        is_active=True
    )
    
    if room_features.count() > 0:
        print(f"✓ Found {room_features.count()} room-related ExtraFeatures:")
        for f in room_features:
            print(f"  {f.feature_code}: quantity={f.quantity}, feature_num={f.feature_number}")
    else:
        print("✗ No room data found in ExtraFeatures (RMB/RMF/RMH)")
        print("  This means bedroom/bathroom data was NOT imported from extra_features.txt")
    
    # 5. Check source files
    print("\n5. SOURCE FILES")
    print("-" * 80)
    
    downloads_path = "/app/downloads"
    extra_features_dirs = [
        "Real_acct_ownership_history",
        "Real_building_land",
        "downloads"
    ]
    
    extra_features_file = None
    for dir_name in extra_features_dirs:
        dir_path = os.path.join(downloads_path, dir_name)
        if os.path.exists(dir_path):
            files = os.listdir(dir_path)
            extra_files = [f for f in files if 'extra_features' in f.lower() or 'fixtures' in f.lower()]
            if extra_files:
                print(f"✓ Found in {dir_name}/:")
                for f in extra_files:
                    file_path = os.path.join(dir_path, f)
                    size = os.path.getsize(file_path)
                    print(f"  - {f} ({size:,} bytes)")
                    if 'extra_features' in f.lower():
                        extra_features_file = file_path
    
    if extra_features_file:
        print(f"\n  Checking {os.path.basename(extra_features_file)} for account {account}...")
        try:
            with open(extra_features_file, 'r', encoding='latin-1') as f:
                lines = [line for line in f if account in line]
            
            print(f"  Found {len(lines)} total lines with account {account}")
            
            # Check for room codes
            room_lines = [line for line in lines if any(code in line for code in room_codes)]
            if room_lines:
                print(f"  ✓ Found {len(room_lines)} lines with room codes:")
                for line in room_lines[:10]:
                    print(f"    {line.strip()}")
            else:
                print(f"  ✗ No room codes (RMB/RMF/RMH) found in source file")
                print(f"  Sample lines from file:")
                for line in lines[:5]:
                    print(f"    {line.strip()}")
        except Exception as e:
            print(f"  ✗ Error reading file: {e}")
    else:
        print("\n  ✗ No extra_features.txt file found")
    
    # 6. Check fixtures.txt for room data
    print("\n6. FIXTURES.TXT CHECK")
    print("-" * 80)
    
    fixtures_file = os.path.join(downloads_path, "Real_building_land", "fixtures.txt")
    if os.path.exists(fixtures_file):
        print(f"✓ Found fixtures.txt")
        try:
            with open(fixtures_file, 'r', encoding='latin-1') as f:
                lines = [line for line in f if account in line]
            
            room_lines = [line for line in lines if any(code in line for code in room_codes)]
            if room_lines:
                print(f"✓ Found {len(room_lines)} room records in fixtures.txt:")
                for line in room_lines:
                    print(f"    {line.strip()}")
            else:
                print("✗ No room records found in fixtures.txt")
        except Exception as e:
            print(f"✗ Error reading fixtures.txt: {e}")
    else:
        print("✗ fixtures.txt not found")
    
    # 7. Summary and recommendations
    print("\n7. SUMMARY & RECOMMENDATIONS")
    print("=" * 80)
    
    if buildings.count() == 0:
        print("✗ ISSUE: No BuildingDetail records found")
        print("  ACTION: Run building data import:")
        print("    docker compose exec web python manage.py import_building_data")
    elif buildings.first().bedrooms is None:
        print("✗ ISSUE: BuildingDetail exists but bedrooms/bathrooms are NULL")
        
        # Check if room data is in fixtures.txt
        if os.path.exists(fixtures_file):
            try:
                with open(fixtures_file, 'r', encoding='latin-1') as f:
                    fixture_lines = [line for line in f if account in line and any(code in line for code in room_codes)]
                
                if fixture_lines:
                    print("✓ Room data EXISTS in fixtures.txt")
                    print("  DIAGNOSIS: fixtures.txt was not processed during last import")
                    print("  SOLUTION: Re-run the import OR run load_room_counts command")
                    print()
                    print("  Option 1 - Full reimport (slow):")
                    print("    docker compose exec web python manage.py import_building_data --skip-download")
                    print()
                    print("  Option 2 - Just update room counts (fast):")
                    print("    docker compose exec web python manage.py load_room_counts")
                else:
                    print("✗ Room data NOT in fixtures.txt")
                    print("  DIAGNOSIS: Data missing from source file")
            except Exception:
                pass
        else:
            print("✗ fixtures.txt not found")
            print("  ACTION: Download and import building data")
            print("    docker compose exec web python manage.py import_building_data")
    else:
        print("✓ Data looks good!")
        print(f"  Bedrooms: {buildings.first().bedrooms}")
        print(f"  Bathrooms: {buildings.first().bathrooms}")
        print(f"  Half Baths: {buildings.first().half_baths}")


if __name__ == "__main__":
    main()
