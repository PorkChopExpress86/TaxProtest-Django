#!/usr/bin/env python
"""
Check the HTML output of the similar properties view.
Run this with: docker compose exec web python scripts/check_html_output.py
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
import re


def main():
    account = "1074380000028"
    
    print("=" * 80)
    print(f"CHECKING HTML OUTPUT FOR ACCOUNT {account}")
    print("=" * 80)
    
    # Create a mock request
    factory = RequestFactory()
    request = factory.get(f'/similar/{account}/')
    
    # Call the view
    response = similar_properties(request, account)
    html = response.content.decode('utf-8')
    
    print("\n✓ Checking HTML content:")
    print(f"  'Price per Sqft:' in header: {'Price per Sqft:' in html}")
    print(f"  '$/Sqft' column header: {'$/Sqft' in html}")
    print(f"  'YOUR PROPERTY' label: {'YOUR PROPERTY' in html}")
    print(f"  'percentile' text: {'percentile' in html}")
    
    # Extract price per sqft value
    ppsf_match = re.search(r'Price per Sqft:.*?\$(\d+\.\d+)', html)
    if ppsf_match:
        print(f"\n✓ Found price per sqft in header: ${ppsf_match.group(1)}")
    else:
        print(f"\n✗ Price per sqft value not found in header")
    
    # Extract percentile
    percentile_match = re.search(r'\((\d+)(?:st|nd|rd|th) percentile\)', html)
    if percentile_match:
        print(f"✓ Found percentile: {percentile_match.group(1)}th percentile")
    else:
        print(f"✗ Percentile not found")
    
    # Count rows with is_target styling
    target_rows = html.count('bg-indigo-50')
    print(f"\n✓ Rows with target property styling: {target_rows}")
    
    # Count total table rows
    tbody_match = re.search(r'<tbody[^>]*>(.*?)</tbody>', html, re.DOTALL)
    if tbody_match:
        tbody_content = tbody_match.group(1)
        row_count = tbody_content.count('<tr')
        print(f"✓ Total rows in results table: {row_count}")
    
    print("\n" + "=" * 80)
    print("HTML check complete!")
    print("=" * 80)


if __name__ == "__main__":
    main()
