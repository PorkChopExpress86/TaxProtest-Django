#!/usr/bin/env python
"""Test the improved similarity algorithm"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from data.similarity import find_similar_properties

print('Testing improved similarity search for account 1074380000028...')
results = find_similar_properties('1074380000028', max_results=10)

print(f'\nFound {len(results)} similar properties:')
print('\nTop 10 Results:')
print('-' * 110)
print(f"{'Address':<40} {'Score':<8} {'Distance':<10} {'Sqft':<8} {'Bed/Bath':<10} {'PPSF':<10}")
print('-' * 110)

for r in results[:10]:
    p = r['property']
    b = r['building']
    addr = f'{p.street_number} {p.street_name}'[:38]
    score = r['similarity_score']
    dist = r['distance']
    sqft = int(b.heat_area) if b and b.heat_area else 0
    bedbath = f"{b.bedrooms or 0}/{b.bathrooms or 0}"
    ppsf = p.assessed_value / b.heat_area if (b and b.heat_area and p.assessed_value) else 0
    
    print(f'{addr:<40} {score:<8.1f} {dist:<10.2f} {sqft:<8} {bedbath:<10} ${ppsf:<9.2f}')

if len(results) >= 3:
    print('\nScore distribution:')
    scores = [r['similarity_score'] for r in results]
    print(f'  Highest: {max(scores):.1f}')
    print(f'  Median: {sorted(scores)[len(scores)//2]:.1f}')
    print(f'  Lowest: {min(scores):.1f}')
    print(f'  Above 70: {sum(1 for s in scores if s >= 70)}')
    print(f'  Above 60: {sum(1 for s in scores if s >= 60)}')
    print(f'  Above 50: {sum(1 for s in scores if s >= 50)}')
    
    print('\nDistance distribution:')
    distances = [r['distance'] for r in results]
    print(f'  Closest: {min(distances):.2f} miles')
    print(f'  Median: {sorted(distances)[len(distances)//2]:.2f} miles')
    print(f'  Farthest: {max(distances):.2f} miles')
    print(f'  Within 5 miles: {sum(1 for d in distances if d <= 5)}')
    print(f'  Within 3 miles: {sum(1 for d in distances if d <= 3)}')
