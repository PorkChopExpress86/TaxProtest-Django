#!/usr/bin/env python
"""Check specific RR feature codes."""
import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taxprotest.settings")
django.setup()

from data.models import ExtraFeature

# Check specific codes
codes = ["RRG1", "RRPS", "RRP9", "RRG2", "RRG3", "RRP1", "RRP2", "RRP3"]

print("Residential RR Feature Codes:")
print("=" * 80)
for code in codes:
    f = ExtraFeature.objects.filter(feature_code=code, is_active=True).first()
    if f:
        print(f"{code:10s}: {f.feature_description}")
    else:
        print(f"{code:10s}: Not found")
