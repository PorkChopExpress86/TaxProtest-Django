#!/usr/bin/env python
"""Test if template renders protest recommendation"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from django.template import Context
from django.template.loader import get_template

# Simulate the context with protest recommendation
context_data = {
    'target_property': type('obj', (object,), {
        'account_number': '1074380000028',
        'assessed_value': 349000,
        'street_number': '16213',
        'street_name': 'WALL ST',
        'zipcode': '77040'
    }),
    'target_building': None,
    'target_features': '',
    'target_year_built': 1978,
    'target_bedrooms': 4,
    'target_bathrooms': 2.5,
    'target_quality_code': 'C',
    'target_area': 2236,
    'target_ppsf': 156.08,
    'protest_recommendation': 'Recommend protesting',
    'protest_recommendation_level': 'strong',
    'protest_recommendation_reason': 'Your price per sqft ($156.08) is about 25% above the median ($125.00)',
    'ppsf_median': 125.00,
    'ppsf_average': 130.00,
    'ppsf_min': 100.00,
    'ppsf_max': 180.00,
    'comparable_count': 10,
    'comparable_avg_score': 65,
    'results': [],
    'max_distance': 10,
    'max_results': 50,
    'min_score': 30
}

# Load the template
template = get_template('similar_properties.html')

# Render
html = template.render(context_data)

# Check if recommendation HTML is in the output
print("Checking rendered HTML...")
print()

if 'Recommend protesting' in html:
    print('✅ Protest recommendation IS in the HTML')
    print('   Found text: "Recommend protesting"')
elif 'protest_recommendation' in html:
    print('⚠️  Template variable name in HTML (not rendered properly)')
else:
    print('❌ Protest recommendation NOT in HTML')

print()

# Check for various banner elements
checks = [
    ('bg-red-50', 'Strong recommendation banner (Tailwind red)'),
    ('bg-amber-50', 'Moderate recommendation banner (Tailwind amber)'),
    ('bg-green-50', 'Low recommendation banner (Tailwind green)'),
    ('text-red-900', 'Strong recommendation text color'),
    ('Your PPSF', 'Your PPSF label'),
    ('Comp Median', 'Median PPSF label'),
    ('Based on', 'Comparable count text'),
]

for search_text, description in checks:
    if search_text in html:
        print(f'✅ Found: {description}')
    else:
        print(f'❌ Missing: {description}')

print()
print(f"Total HTML length: {len(html)} characters")

# Extract the protest banner section if it exists
if 'protest_recommendation' in html.lower():
    # Find the section
    start = html.lower().find('{% if protest_recommendation %}')
    if start == -1:
        start = html.lower().find('recommend protesting')
    if start > 0:
        snippet = html[max(0, start-100):min(len(html), start+500)]
        print("\nSnippet around protest recommendation:")
        print(snippet[:300] + "...")
