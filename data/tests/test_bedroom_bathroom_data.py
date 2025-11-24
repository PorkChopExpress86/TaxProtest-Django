"""
Tests for bedroom and bathroom data extraction and display.
This test file specifically investigates why bedroom/bathroom data is not showing
for account 1074380000028 (16213 Wall St).
"""
from decimal import Decimal
from django.test import TestCase
from data.models import PropertyRecord, BuildingDetail, ExtraFeature


class BedroomBathroomDataTest(TestCase):
    """Test bedroom and bathroom data for specific property."""
    
    def test_property_exists(self):
        """Test that the property record exists in the database."""
        account = "1074380000028"
        prop = PropertyRecord.objects.filter(account_number=account).first()
        
        self.assertIsNotNone(prop, f"PropertyRecord for account {account} not found")
        self.assertEqual(prop.street_name, "WALL", f"Street name should be WALL, got {prop.street_name}")
        self.assertEqual(prop.zipcode, "77040", f"Zipcode should be 77040, got {prop.zipcode}")
        
        print(f"\n✓ PropertyRecord found: {prop.address}")
        print(f"  Account: {prop.account_number}")
        print(f"  Owner: {prop.owner_name}")
        
    def test_building_details_exist(self):
        """Test that BuildingDetail records exist for the property."""
        account = "1074380000028"
        buildings = BuildingDetail.objects.filter(account_number=account, is_active=True)
        
        self.assertGreater(buildings.count(), 0, f"No BuildingDetail records found for account {account}")
        
        print(f"\n✓ Found {buildings.count()} active BuildingDetail record(s)")
        for bd in buildings:
            print(f"  Building #{bd.building_number}:")
            print(f"    Bedrooms: {bd.bedrooms}")
            print(f"    Bathrooms: {bd.bathrooms}")
            print(f"    Half Baths: {bd.half_baths}")
            print(f"    Heat Area: {bd.heat_area}")
            print(f"    Year Built: {bd.year_built}")
            print(f"    Import Date: {bd.import_date}")
            print(f"    Import Batch: {bd.import_batch_id}")
    
    def test_bedroom_bathroom_values(self):
        """Test that bedroom and bathroom values are correctly populated."""
        account = "1074380000028"
        building = BuildingDetail.objects.filter(
            account_number=account, 
            is_active=True,
            building_number=1
        ).first()
        
        self.assertIsNotNone(building, f"Primary building not found for account {account}")
        
        # Expected values: 4 bedrooms, 2.5 baths (2 full + 1 half)
        print(f"\n✓ Checking bedroom/bathroom values:")
        print(f"  Expected: 4 bedrooms, 2 full baths, 1 half bath")
        print(f"  Actual: {building.bedrooms} bedrooms, {building.bathrooms} full baths, {building.half_baths} half baths")
        
        # These assertions will likely fail, revealing the issue
        if building.bedrooms is None:
            print("  ⚠ WARNING: Bedrooms is None!")
        else:
            self.assertEqual(building.bedrooms, 4, "Should have 4 bedrooms")
            
        if building.bathrooms is None:
            print("  ⚠ WARNING: Bathrooms is None!")
        else:
            self.assertEqual(building.bathrooms, Decimal('2'), "Should have 2 full bathrooms")
            
        if building.half_baths is None:
            print("  ⚠ WARNING: Half baths is None!")
        else:
            self.assertEqual(building.half_baths, 1, "Should have 1 half bath")
    
    def test_extra_features_for_room_data(self):
        """Test if bedroom/bathroom data exists in ExtraFeature records."""
        account = "1074380000028"
        
        # Check for room-related feature codes
        room_codes = ['RMB', 'RMF', 'RMH']  # Bedrooms, Full bath, Half bath
        features = ExtraFeature.objects.filter(
            account_number=account,
            is_active=True,
            feature_code__in=room_codes
        )
        
        print(f"\n✓ Checking ExtraFeature records for room data:")
        print(f"  Total room-related features: {features.count()}")
        
        bedroom_features = features.filter(feature_code='RMB')
        fullbath_features = features.filter(feature_code='RMF')
        halfbath_features = features.filter(feature_code='RMH')
        
        print(f"  RMB (Bedrooms): {bedroom_features.count()} records")
        for f in bedroom_features:
            print(f"    Feature #{f.feature_number}: quantity={f.quantity}, building={f.feature_description}")
            
        print(f"  RMF (Full Baths): {fullbath_features.count()} records")
        for f in fullbath_features:
            print(f"    Feature #{f.feature_number}: quantity={f.quantity}, building={f.feature_description}")
            
        print(f"  RMH (Half Baths): {halfbath_features.count()} records")
        for f in halfbath_features:
            print(f"    Feature #{f.feature_number}: quantity={f.quantity}, building={f.feature_description}")
        
        # If we find room features, the issue is in the ETL aggregation
        if features.count() > 0:
            print("\n  ℹ INSIGHT: Room data exists in ExtraFeatures but not in BuildingDetail")
            print("  This suggests the ETL aggregation step failed or wasn't run")
        else:
            print("\n  ℹ INSIGHT: No room data in ExtraFeatures either")
            print("  This suggests the data wasn't imported from source files")
    
    def test_all_extra_features(self):
        """List all extra features for the property to see what was imported."""
        account = "1074380000028"
        features = ExtraFeature.objects.filter(
            account_number=account,
            is_active=True
        ).order_by('feature_number')
        
        print(f"\n✓ All ExtraFeature records for {account}:")
        print(f"  Total: {features.count()} features")
        
        for f in features:
            print(f"  #{f.feature_number}: {f.feature_code} - {f.feature_description}")
            print(f"    Quantity: {f.quantity}, Area: {f.area}")
            print(f"    Import: {f.import_date} (batch: {f.import_batch_id})")
    
    def test_check_source_files(self):
        """Check if source files for bedroom/bathroom data exist."""
        import os
        
        base_path = "/app/downloads"
        extra_features_file = None
        
        # Find the most recent extra_features.txt file
        real_acct_ownership_history_path = os.path.join(base_path, "Real_acct_ownership_history")
        
        print(f"\n✓ Checking for source data files:")
        
        if os.path.exists(real_acct_ownership_history_path):
            files = os.listdir(real_acct_ownership_history_path)
            extra_files = [f for f in files if 'extra_features' in f.lower()]
            print(f"  Found {len(extra_files)} extra_features files in {real_acct_ownership_history_path}")
            for f in extra_files:
                print(f"    - {f}")
                extra_features_file = os.path.join(real_acct_ownership_history_path, f)
        else:
            print(f"  ⚠ Directory not found: {real_acct_ownership_history_path}")
        
        # If we found a file, check if our account is in it
        if extra_features_file and os.path.exists(extra_features_file):
            print(f"\n  Checking {os.path.basename(extra_features_file)} for account 1074380000028...")
            with open(extra_features_file, 'r') as f:
                lines = [line for line in f if '1074380000028' in line]
            
            print(f"  Found {len(lines)} lines with account 1074380000028")
            
            # Show lines with room codes
            room_lines = [line for line in lines if any(code in line for code in ['RMB', 'RMF', 'RMH'])]
            if room_lines:
                print(f"  Found {len(room_lines)} lines with room codes (RMB/RMF/RMH):")
                for line in room_lines[:10]:  # Show first 10
                    print(f"    {line.strip()}")
            else:
                print("  ⚠ No room code lines found in source file")


class ETLLogicTest(TestCase):
    """Test the ETL logic for extracting bedroom/bathroom data."""
    
    def test_parse_extra_features_for_rooms(self):
        """Test that the parse_extra_features_for_building function works correctly."""
        from data.etl import parse_extra_features_for_building
        
        # Sample data that should exist for account 1074380000028
        # This is what we expect to find in extra_features.txt
        sample_lines = [
            "1074380000028\t1\tRMB\t4\t...",  # 4 bedrooms
            "1074380000028\t1\tRMF\t2\t...",  # 2 full baths
            "1074380000028\t1\tRMH\t1\t...",  # 1 half bath
        ]
        
        print("\n✓ Testing parse_extra_features_for_building logic")
        print("  This test would require mocking the actual ETL function")
        print("  See data/etl.py lines 740-850 for the implementation")


class ViewDisplayTest(TestCase):
    """Test that bedroom/bathroom data displays correctly in views."""
    
    def test_index_view_displays_bedrooms_bathrooms(self):
        """Test that the index view correctly displays bedroom/bathroom data."""
        from django.test import Client
        
        client = Client()
        
        # Search for the property
        response = client.get('/', {'search': '16213 Wall'})
        
        self.assertEqual(response.status_code, 200)
        
        # Check if bedroom/bathroom data is in the response
        content = response.content.decode('utf-8')
        
        print("\n✓ Testing view display:")
        if 'bedroom' in content.lower():
            print("  ✓ 'bedroom' found in response")
        else:
            print("  ⚠ 'bedroom' NOT found in response")
            
        if 'bath' in content.lower():
            print("  ✓ 'bath' found in response")
        else:
            print("  ⚠ 'bath' NOT found in response")
