#!/usr/bin/env python
"""Check feature codes and descriptions in the database."""
import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taxprotest.settings")
django.setup()

from data.models import ExtraFeature

# Get sample of feature codes
features = (
    ExtraFeature.objects.filter(is_active=True)
    .values("feature_code", "feature_description")
    .distinct()[:50]
)

print("Sample Feature Codes and Descriptions:")
print("=" * 80)
for f in features:
    print(f"{f['feature_code']:10s}: {f['feature_description']}")
