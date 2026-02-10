import os
import django
import sys

# Setup Django environment
sys.path.append('/mnt/samsung/Docker/TaxProtest-Django')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from data.models import PropertyRecord

def check_property():
    print("Checking for properties with zip 77040...")
    count_zip = PropertyRecord.objects.filter(zipcode='77040').count()
    print(f"Total properties in 77040: {count_zip}")

    print("\nChecking for properties with street name containing 'Wall'...")
    count_wall = PropertyRecord.objects.filter(street_name__icontains='Wall').count()
    print(f"Total properties with 'Wall' in street name: {count_wall}")

    print("\nChecking for intersection...")
    wall_77040 = PropertyRecord.objects.filter(zipcode='77040', street_name__icontains='Wall')
    print(f"Properties with 'Wall' in street name AND zip 77040: {wall_77040.count()}")

    for p in wall_77040[:5]:
        print(f" - {p.address} (Account: {p.account_number})")

if __name__ == "__main__":
    check_property()
