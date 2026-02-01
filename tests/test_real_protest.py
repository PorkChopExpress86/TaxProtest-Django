#!/usr/bin/env python
"""Test real property with similarity search"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from data.models import PropertyRecord, BuildingDetail
from data.similarity import find_similar_properties

# Test property 1074380000028
account = '1074380000028'
target = PropertyRecord.objects.filter(account_number=account).first()

if not target:
    print(f"Property {account} not found!")
    exit(1)

target_building = target.buildings.filter(is_active=True).first()

print(f"Target Property: {target.street_number} {target.street_name}")
print(f"  Assessed Value: ${target.assessed_value:,.0f}")
print(f"  Heat Area: {target_building.heat_area if target_building else 'N/A'}")

if target_building and target_building.heat_area:
    target_ppsf = float(target.assessed_value / target_building.heat_area)
    print(f"  PPSF: ${target_ppsf:.2f}")
else:
    print("  PPSF: N/A")
    target_ppsf = None

print()

# Find similar properties
print("Finding similar properties...")
results = find_similar_properties(account, max_results=10)

print(f"Found {len(results)} similar properties")

if not results:
    print("No results! Cannot calculate recommendation.")
    exit(0)

# Extract PPSF from comparables
comparable_ppsf = []
for r in results:
    p = r['property']
    b = r['building']
    if b and b.heat_area and p.assessed_value:
        ppsf = p.assessed_value / b.heat_area
        comparable_ppsf.append({
            'ppsf': ppsf,
            'score': r['similarity_score'],
            'address': f"{p.street_number} {p.street_name}"
        })

print(f"\n{len(comparable_ppsf)} comparables with PPSF data:")
for comp in comparable_ppsf[:5]:
    print(f"  {comp['address']}: ${comp['ppsf']:.2f} (score {comp['score']:.1f})")

if len(comparable_ppsf) >= 3 and target_ppsf:
    # Calculate stats
    ppsf_values = [float(c['ppsf']) for c in comparable_ppsf]
    ppsf_values_sorted = sorted(ppsf_values)
    
    mid = len(ppsf_values) // 2
    if len(ppsf_values) % 2 == 1:
        median = ppsf_values_sorted[mid]
    else:
        median = (ppsf_values_sorted[mid-1] + ppsf_values_sorted[mid]) / 2.0
    
    average = sum(ppsf_values) / len(ppsf_values)
    
    over_pct = ((target_ppsf - median) / median) * 100
    
    print(f"\nPPSF Analysis:")
    print(f"  Target PPSF: ${target_ppsf:.2f}")
    print(f"  Median PPSF: ${median:.2f}")
    print(f"  Average PPSF: ${average:.2f}")
    print(f"  Difference: {over_pct:+.1f}%")
    
    print(f"\nRecommendation:")
    if over_pct >= 20:
        print(f"  ✅ STRONGLY RECOMMEND PROTESTING")
        print(f"     Your PPSF is {over_pct:.0f}% above median")
    elif over_pct >= 10:
        print(f"  ⚠️  CONSIDER PROTESTING")
        print(f"     Your PPSF is {over_pct:.0f}% above median")
    elif over_pct <= -10:
        print(f"  ❌ PROTEST NOT RECOMMENDED")
        print(f"     Your PPSF is {abs(over_pct):.0f}% below median")
    else:
        print(f"  ℹ️  BORDERLINE")
        print(f"     Your PPSF is close to median ({over_pct:+.1f}%)")
else:
    print(f"\nInsufficient data for recommendation")
    print(f"  Need 3+ comparables with PPSF, have {len(comparable_ppsf)}")
