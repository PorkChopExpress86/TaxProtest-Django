
import os
import django
from django.conf import settings
from decimal import Decimal

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from data.etl import load_fixtures_room_counts
from data.models import BuildingDetail, PropertyRecord

def verify_rooms():
    print("--- Verifying Room Counts Import ---")
    
    # Create valid building record to match fixtures
    # Fixture sample (from step 57):
    # 0020720000014   1       RMB     Room:  Bedroom  3.00
    # 0020720000014   1       RMF     Room:  Full Bath        3.00
    # 0020720000014   1       RMH     Room:  Half Bath        1.00
    
    acct = "0020720000014"
    if not PropertyRecord.objects.filter(account_number=acct).exists():
        PropertyRecord.objects.create(account_number=acct, address="TEST ADDR 2")
    
    # Ensure building exists
    if not BuildingDetail.objects.filter(account_number=acct, building_number=1).exists():
        BuildingDetail.objects.create(
            property=PropertyRecord.objects.get(account_number=acct),
            account_number=acct,
            building_number=1,
            is_active=True
        )
        print(f"Created dummy building for {acct}")
    
    filepath = "/tmp/test_fixtures_room.txt"
    if not os.path.exists(filepath):
        print("Test file not found")
        return

    # Run import
    results = load_fixtures_room_counts(filepath, chunk_size=10)
    print(f"Import results: {results}")
    
    # Check DB
    b = BuildingDetail.objects.get(account_number=acct, building_number=1)
    print(f"Building Found: Beds={b.bedrooms}, Baths={b.bathrooms}, Half={b.half_baths}")
    
    # Expected: Beds=3, Baths=3.5 (3 + 0.5*1)
    if b.bedrooms == 3 and b.bathrooms == Decimal("3.5"):
         print("SUCCESS: Room counts match expectation.")
    else:
         print("FAILURE: Room counts do not match.")

if __name__ == "__main__":
    verify_rooms()
