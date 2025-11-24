#!/usr/bin/env python
"""
Test the similar properties view with price per square foot.
Run this with: docker compose exec web python scripts/test_similar_properties.py
"""
import os
import sys
import django

# Setup Django
sys.path.insert(0, '/app')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from taxprotest.views import similar_properties
from django.test import RequestFactory
from data.models import PropertyRecord


def main():
    account = "1074380000028"
    
    print("=" * 80)
    print(f"TESTING SIMILAR PROPERTIES VIEW FOR ACCOUNT {account}")
    print("=" * 80)
    
    # Check if property exists and has location data
    prop = PropertyRecord.objects.filter(account_number=account).first()
    if not prop:
        print(f"✗ Property not found")
        return
    
    print(f"\n✓ Property found: {prop.address}")
    print(f"  Latitude: {prop.latitude}")
    print(f"  Longitude: {prop.longitude}")
    
    if not prop.latitude or not prop.longitude:
        print(f"\n✗ Property does not have location data")
        print(f"  Cannot test similarity search without coordinates")
        return
    
    # Create a mock request
    factory = RequestFactory()
    request = factory.get(f'/similar/{account}/', {
        'max_distance': '5',
        'max_results': '20',
        'min_score': '30'
    })
    
    # Call the view
    print(f"\n✓ Calling similar_properties view...")
    try:
        response = similar_properties(request, account)
        print(f"  Status: {response.status_code}")
        
        if response.status_code == 200:
            # Check if the response contains the expected data
            content = response.content.decode('utf-8')
            
            print(f"\n✓ Response generated successfully")
            print(f"  Contains 'YOUR PROPERTY': {'YOUR PROPERTY' in content}")
            print(f"  Contains '$/Sqft': {'$/Sqft' in content or '$/sqft' in content.lower()}")
            print(f"  Contains 'percentile': {'percentile' in content}")
            
            # Try to extract context data
            from django.template import Template, Context
            if hasattr(response, 'context_data') and response.context_data:
                context = response.context_data
                results = context.get('results', [])
                target_ppsf = context.get('target_ppsf')
                target_percentile = context.get('target_ppsf_percentile')
                
                print(f"\n✓ Context data:")
                print(f"  Number of results: {len(results)}")
                print(f"  Target price per sqft: ${target_ppsf:.2f}" if target_ppsf else "  Target price per sqft: None")
                print(f"  Target percentile: {target_percentile:.1f}" if target_percentile else "  Target percentile: None")
                
                if results:
                    print(f"\n✓ First few results:")
                    for i, r in enumerate(results[:3], 1):
                        is_target = r.get('is_target', False)
                        ppsf = r.get('ppsf')
                        print(f"  {i}. {r['address']} {r['street_name']}")
                        if is_target:
                            print(f"     >>> YOUR PROPERTY <<<")
                        else:
                            print(f"     Similarity: {r['similarity_score']}%")
                        print(f"     Price/sqft: ${ppsf:.2f}" if ppsf else "     Price/sqft: None")
            else:
                print(f"\n  Note: Context data not directly accessible in response")
                print(f"  This is normal for render() responses")
        else:
            print(f"\n✗ Unexpected status code: {response.status_code}")
            
    except Exception as e:
        print(f"\n✗ Error calling view: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
