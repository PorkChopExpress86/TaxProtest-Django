"""Regression test for candidate coverage outcome populations performance."""

from __future__ import annotations

import time
from unittest.mock import patch

from django.test import TestCase

from counties.harris.adapter import adapter
from counties.harris.etl_pipeline.candidate import outcome_populations
from counties.harris.models import BuildingDetail, PropertyRecord


class CandidateCoveragePerformanceTests(TestCase):
    def test_outcome_populations_does_not_execute_per_property_comparable_searches(self):
        """Evaluating coverage must not execute an O(N) loop of live comp searches."""
        count = 30
        for i in range(count):
            acct = f"P{i:05d}"
            prop = PropertyRecord.objects.create(
                account_number=acct,
                address=f"{i} MAIN ST",
                is_residential=True,
                is_data_ready=True,
                latitude=29.75,
                longitude=-95.36,
                value=250000,
                assessed_value=250000,
                building_area=1800,
            )
            BuildingDetail.objects.create(
                property=prop,
                account_number=acct,
                building_number=1,
                bedrooms=3,
                bathrooms=2,
                heat_area=1800,
            )

        with patch.object(adapter, "find_comps", wraps=adapter.find_comps) as mock_find_comps:
            start_time = time.monotonic()
            populations = outcome_populations(2026)
            duration = time.monotonic() - start_time

            # The current bug calls adapter.find_comps 30 times (once per property)
            # which scales to 1.17M calls (32+ hours) in production.
            # It must not call find_comps per property.
            self.assertEqual(
                mock_find_comps.call_count,
                0,
                f"outcome_populations called find_comps {mock_find_comps.call_count} times in an O(N) loop",
            )
            self.assertLess(duration, 1.0, f"Took {duration:.2f}s for {count} properties")
            self.assertIn("report", populations)
            self.assertIn("search", populations)
