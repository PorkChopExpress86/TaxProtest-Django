#!/usr/bin/env python
import os
import sys
import django

# Setup Django environment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taxprotest.settings")
django.setup()

from data.models import PropertyRecord
from django.db.models import Count

# Find duplicate account numbers
dupes = (
    PropertyRecord.objects.values("account_number")
    .annotate(count=Count("id"))
    .filter(count__gt=1)
    .order_by("-count")
)

print(f"Total duplicate account_numbers: {dupes.count()}")
print("\nTop 10 duplicates:")
for d in dupes[:10]:
    print(f"  {d['account_number']}: {d['count']} records")

# Check specific case: Wall street in 77040
print("\n\nChecking Wall street in 77040:")
wall_props = PropertyRecord.objects.filter(
    street_name__icontains="Wall", zipcode="77040"
).order_by("account_number", "id")

print(f"Found {wall_props.count()} properties")
for prop in wall_props:
    buildings = prop.buildings.filter(is_active=True)
    building = buildings.first()
    quality = building.quality_code if building else None
    print(
        f"  ID: {prop.id}, Account: {prop.account_number}, Address: {prop.street_number} {prop.street_name}, Quality: {quality}, Building count: {buildings.count()}"
    )
