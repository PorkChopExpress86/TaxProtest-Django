#!/usr/bin/env python
import os
import sys
import django

# Setup Django environment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taxprotest.settings")
django.setup()

from data.models import PropertyRecord, ExtraFeature

# Check if ExtraFeature table has any data
total_features = ExtraFeature.objects.count()
active_features = ExtraFeature.objects.filter(is_active=True).count()

print(f"Total ExtraFeatures: {total_features:,}")
print(f"Active ExtraFeatures: {active_features:,}")

if total_features > 0:
    print("\nSample features:")
    for feat in ExtraFeature.objects.filter(is_active=True)[:10]:
        print(
            f"  Account: {feat.account_number}, Code: {feat.feature_code}, Desc: {feat.feature_description}"
        )

# Check specific properties from Wall street
print("\n\nChecking Wall street properties in 77040:")
wall_props = PropertyRecord.objects.filter(
    street_name__icontains="Wall", zipcode="77040"
)[:5]

for prop in wall_props:
    features = prop.extra_features.filter(is_active=True)
    print(f"\nAccount {prop.account_number} ({prop.street_number} {prop.street_name}):")
    print(f"  Features count: {features.count()}")
    for feat in features:
        print(f"    - {feat.feature_description} ({feat.feature_code})")
