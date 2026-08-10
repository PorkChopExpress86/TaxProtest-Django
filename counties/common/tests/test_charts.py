"""counties/common/charts.py: pure SVG/summary layout helpers."""

from __future__ import annotations

from django.test import SimpleTestCase

from counties.common.charts import score_breakdown_summary
from counties.common.contracts import ScoreComponent


class ScoreBreakdownSummaryTests(SimpleTestCase):
    def test_formats_each_scored_component(self):
        components = [
            ScoreComponent(
                name="living_area", label="Living Area", weight=24.0, similarity=0.9, points=21.6
            ),
            ScoreComponent(
                name="bedrooms", label="Bedrooms", weight=14.0, similarity=1.0, points=14.0
            ),
        ]

        summary = score_breakdown_summary(components)

        self.assertEqual(summary, "Living Area: 21.6/24.0; Bedrooms: 14.0/14.0")

    def test_skips_components_with_no_points(self):
        # A factor neither property had data for -- points is None, not 0.
        components = [
            ScoreComponent(
                name="living_area", label="Living Area", weight=24.0, similarity=0.9, points=21.6
            ),
            ScoreComponent(
                name="stories", label="Stories", weight=4.0, similarity=None, points=None
            ),
        ]

        summary = score_breakdown_summary(components)

        self.assertEqual(summary, "Living Area: 21.6/24.0")
        self.assertNotIn("Stories", summary)

    def test_empty_list_gives_an_empty_string(self):
        self.assertEqual(score_breakdown_summary([]), "")
