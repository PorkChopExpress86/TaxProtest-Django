
from data.etl import load_building_details
from data.models import BuildingDetail

def test_import():
    filepath = '/tmp/test_building.txt'
    print("--- Before Import ---")
    print(f"Building Count: {BuildingDetail.objects.count()}")

    print("--- Running Import ---")
    # We use a dummy batch ID
    results = load_building_details(filepath, chunk_size=10, import_batch_id='debug_test')
    print(f"Results: {results}")

    print("--- After Import ---")
    print(f"Building Count: {BuildingDetail.objects.count()}")
    if BuildingDetail.objects.count() > 0:
        b = BuildingDetail.objects.first()
        print(f"Sample: {b.account_number} Beds: {b.bedrooms} Quality: {b.quality_code} Year: {b.year_built}")

test_import()
