#!/usr/bin/env python
"""Test the fixtures aggregator"""
import os
import django
from pathlib import Path

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')
django.setup()

from data.etl_pipeline.fixtures_aggregator import FixturesAggregator

# Test loading fixtures
fixtures_path = Path('/app/extracted/Real_building_land/fixtures.txt')
print(f"Loading fixtures from: {fixtures_path}")
print(f"File exists: {fixtures_path.exists()}")

aggregator = FixturesAggregator()
aggregator.load_fixtures_file(fixtures_path)

# Get stats
stats = aggregator.get_stats()
print(f"\nFixtures Statistics:")
print(f"  Total buildings: {stats['total_buildings']:,}")
print(f"  With bedrooms: {stats['with_bedrooms']:,}")
print(f"  With full baths: {stats['with_full_baths']:,}")
print(f"  With half baths: {stats['with_half_baths']:,}")
print(f"  With bathrooms: {stats['with_bathrooms']:,}")
print(f"  With both: {stats['with_both']:,}")

# Test specific property (1074380000028)
print(f"\nTest property 1074380000028:")
fixtures = aggregator.get_fixtures('1074380000028', 1)
print(f"  Bedrooms: {fixtures['bedrooms']}")
print(f"  Full baths: {fixtures['full_baths']}")
print(f"  Half baths: {fixtures['half_baths']}")
print(f"  Total baths: {aggregator.get_bathroom_count('1074380000028', 1)}")

# Test a few more random properties
print(f"\nSample properties:")
for acct in ['0011200000014', '0020720000014', '0021440000001']:
    fixtures = aggregator.get_fixtures(acct, 1)
    bathrooms = aggregator.get_bathroom_count(acct, 1)
    print(f"  {acct}: {fixtures['bedrooms']:.0f} bed, {bathrooms:.1f} bath")
