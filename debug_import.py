
import os
import django
from django.conf import settings

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tax_protest.settings')
django.setup()

from data.etl import load_building_details
from data.models import BuildingDetail

def test_import():
    filepath = 'downloads/Real_building_land/test_building.txt'
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return

    print("--- Before Import ---")
    print(f"Building Count: {BuildingDetail.objects.count()}")

    print("--- Running Import ---")
    results = load_building_details(filepath, chunk_size=10)
    print(f"Results: {results}")

    print("--- After Import ---")
    print(f"Building Count: {BuildingDetail.objects.count()}")
    if BuildingDetail.objects.count() > 0:
        b = BuildingDetail.objects.first()
        print(f"Sample: {b.account_number} Beds: {b.bedrooms} Quality: {b.quality_code}")

if __name__ == '__main__':
    test_import()
