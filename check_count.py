
import os
import django
import sys

# Setup Django environment
sys.path.append('/mnt/samsung/Docker/TaxProtest-Django')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from data.models import PropertyRecord

def check_count():
    print("Checking total property count...")
    count = PropertyRecord.objects.count()
    print(f"Total properties in DB: {count}")
    
    if count > 0:
        print("First 5 properties:")
        for p in PropertyRecord.objects.all()[:5]:
            print(f"- {p.address} (Zip: {p.zipcode})")

if __name__ == "__main__":
    check_count()
