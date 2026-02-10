
import os
import django
from django.conf import settings

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from data.etl import load_extra_features
from data.models import ExtraFeature, PropertyRecord

def verify_fix():
    print("--- Verifying Extra Features Import ---")
    
    # Create a dummy property to attach features to
    acct = "0020720000014" # From the header sample in step 53
    # Ensure it exists
    if not PropertyRecord.objects.filter(account_number=acct).exists():
        PropertyRecord.objects.create(account_number=acct, address="TEST ADDR")
        print(f"Created dummy property {acct}")
    
    filepath = "/tmp/test_features.txt"
    if not os.path.exists(filepath):
        print("Test file not found")
        return

    # Run import
    results = load_extra_features(filepath, truncate=True)
    print(f"Import results: {results}")
    
    # Check DB
    feat = ExtraFeature.objects.filter(account_number=acct).first()
    if feat:
        print(f"Feature Found: Code={feat.feature_code}, Desc={feat.feature_description}, Width={feat.width}, Length={feat.length}")
    else:
        print("No features found for account.")

if __name__ == "__main__":
    verify_fix()
