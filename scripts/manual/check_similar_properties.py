#!/usr/bin/env python
"""
Smoke-check the Harris comparables page against real imported data.
Run this with: docker compose exec web python scripts/manual/check_similar_properties.py
"""

import os
import sys

import django

# Setup Django
sys.path.insert(0, "/app")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taxprotest.settings")
django.setup()

from django.test import RequestFactory

from counties.common.tax_models import ParcelGeometry
from counties.common.views import similar_properties
from counties.harris.adapter import adapter
from counties.harris.models import PropertyRecord


def main():
    account = "1074380000028"

    print("=" * 80)
    print(f"TESTING SIMILAR PROPERTIES VIEW FOR ACCOUNT {account}")
    print("=" * 80)

    prop = PropertyRecord.objects.filter(account_number=account).first()
    if not prop:
        print("✗ Property not found")
        return

    print(f"\n✓ Property found: {prop.address}")

    geom = ParcelGeometry.objects.filter(account_number=account, county="harris").first()
    print(f"  Latitude: {geom.latitude if geom else None}")
    print(f"  Longitude: {geom.longitude if geom else None}")

    if not geom or not geom.latitude or not geom.longitude:
        print("\n✗ Property does not have location data")
        print("  Cannot test similarity search without coordinates")
        return

    factory = RequestFactory()
    request = factory.get(
        f"/similar/{account}/", {"max_distance": "5", "max_results": "20", "min_score": "30"}
    )

    print("\n✓ Calling similar_properties view...")
    try:
        response = similar_properties(request, account, adapter=adapter)
        print(f"  Status: {response.status_code}")

        if response.status_code != 200:
            print(f"\n✗ Unexpected status code: {response.status_code}")
            return

        content = response.content.decode("utf-8")
        print("\n✓ Response generated successfully")
        print(f"  Contains 'YOUR PROPERTY': {'YOUR PROPERTY' in content}")
        print(f"  Contains '$/Sqft': {'$/Sqft' in content or '$/sqft' in content.lower()}")
        print(f"  Contains 'percentile': {'percentile' in content}")

        # render() responses expose their context only via the test client, so
        # re-derive the interesting numbers straight from the adapter.
        subject = adapter.get_subject(account)
        comps = adapter.find_comps(account, max_distance_miles=5.0, max_results=20, min_score=30.0)
        ppsf = subject.value_per_sqft

        print("\n✓ Adapter output:")
        print(f"  Number of comparables: {len(comps)}")
        print(
            f"  Subject price per sqft: ${ppsf:.2f}" if ppsf else "  Subject price per sqft: None"
        )

        for index, comp in enumerate(comps[:3], 1):
            comp_ppsf = comp.value_per_sqft
            print(f"  {index}. {comp.address}")
            print(f"     Similarity: {comp.similarity_score} ({comp.match_label})")
            print(f"     Price/sqft: ${comp_ppsf:.2f}" if comp_ppsf else "     Price/sqft: None")

    except Exception as e:
        print(f"\n✗ Error calling view: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
