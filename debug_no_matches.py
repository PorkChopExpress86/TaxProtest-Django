import os
import django
import sys
from django.conf import settings

# Setup Django environment
sys.path.append('/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from data.models import PropertyRecord, BuildingDetail
from data.similarity import find_similar_properties, calculate_similarity_score

target_acct = '1074380000028'
print(f"--- Debugging Similarity for {target_acct} ---")

try:
    target = PropertyRecord.objects.get(account_number=target_acct)
    print(f"Target: {target.address}")
    print(f"Location: {target.latitude}, {target.longitude}")
    print(f"Land Area: {target.land_area}")
    
    building = BuildingDetail.objects.filter(account_number=target_acct).first()
    if building:
        print(f"Building: {building.heat_area} sqft, Year: {building.year_built}")
    else:
        print("Building: None (Land Only Mode)")
        
    print("\n--- Running Search (10 miles, min_score=0) ---")
    # Run with min_score=0 to see EVERYTHING and why they score low
    results = find_similar_properties(target_acct, max_distance_miles=10.0, min_score=0.0, max_results=10)
    
    print(f"Found {len(results)} results with min_score=0")
    
    for i, res in enumerate(results[:5]):
        print(f"\nResult {i+1}: {res['property'].account_number} - {res['property'].address}")
        print(f"  Distance: {res['distance']} miles")
        print(f"  Score: {res['similarity_score']}")
        print(f"  Land Area: {res['property'].land_area}")
        if res['building']:
             print(f"  Building: {res['building'].heat_area} sqft")
        else:
             print("  Building: None")
             
except Exception as e:
    print(f"Error: {e}")
