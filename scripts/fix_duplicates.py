#!/usr/bin/env python
"""
Fix duplicate PropertyRecord entries by keeping the record with building details
and deleting the duplicate without building details.
"""
import os
import sys
import django

# Setup Django environment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taxprotest.settings")
django.setup()

from data.models import PropertyRecord
from django.db.models import Count
from django.db import transaction

print("Finding duplicate account numbers...")

# Find all duplicate account numbers
dupes = (
    PropertyRecord.objects.values("account_number")
    .annotate(count=Count("id"))
    .filter(count__gt=1)
    .order_by("account_number")
)

total_dupes = dupes.count()
print(f"Found {total_dupes:,} account numbers with duplicates")

if total_dupes == 0:
    print("No duplicates found!")
    sys.exit(0)

# Process each duplicate account number
deleted_count = 0
kept_count = 0
batch_size = 1000
batch = []

print("\nProcessing duplicates...")
print("Strategy: Keep record with building details, delete the other")

with transaction.atomic():
    for idx, dupe in enumerate(dupes, start=1):
        account_num = dupe["account_number"]

        # Get all records for this account
        records = list(
            PropertyRecord.objects.filter(account_number=account_num)
            .prefetch_related("buildings")
            .order_by("id")
        )

        if len(records) != 2:
            # If there are more than 2 duplicates, keep the oldest and delete the rest
            records_to_delete = records[1:]
        else:
            # Standard case: 2 records
            record1, record2 = records

            # Check which has building details
            has_buildings_1 = record1.buildings.filter(is_active=True).exists()
            has_buildings_2 = record2.buildings.filter(is_active=True).exists()

            if has_buildings_1 and not has_buildings_2:
                # Keep record1, delete record2
                records_to_delete = [record2]
            elif has_buildings_2 and not has_buildings_1:
                # Keep record2, delete record1
                records_to_delete = [record1]
            elif has_buildings_1 and has_buildings_2:
                # Both have buildings, keep the older one (smaller ID)
                records_to_delete = [record2]
            else:
                # Neither has buildings, keep the older one (smaller ID)
                records_to_delete = [record2]

        # Add to deletion batch
        for rec in records_to_delete:
            batch.append(rec.id)
            deleted_count += 1

        kept_count += 1

        # Delete in batches
        if len(batch) >= batch_size:
            PropertyRecord.objects.filter(id__in=batch).delete()
            print(
                f"Progress: {idx:,}/{total_dupes:,} ({idx/total_dupes*100:.1f}%) - Deleted {deleted_count:,}, Kept {kept_count:,}"
            )
            batch.clear()

    # Delete remaining batch
    if batch:
        PropertyRecord.objects.filter(id__in=batch).delete()

print(f"\nComplete!")
print(f"  Deleted: {deleted_count:,} duplicate records")
print(f"  Kept: {kept_count:,} unique records")

# Verify
remaining_dupes = (
    PropertyRecord.objects.values("account_number")
    .annotate(count=Count("id"))
    .filter(count__gt=1)
    .count()
)

print(f"\nVerification: {remaining_dupes:,} account numbers still have duplicates")

if remaining_dupes == 0:
    print("✅ All duplicates successfully removed!")
else:
    print("⚠️  Some duplicates remain - may need manual investigation")
