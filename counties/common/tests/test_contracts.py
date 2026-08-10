"""counties/common/contracts.py: the county-neutral record shapes."""

from __future__ import annotations

from django.test import SimpleTestCase

from counties.common.contracts import ScoreComponent


class ScoreComponentFromMappingTests(SimpleTestCase):
    def test_builds_from_a_similarity_module_component_dict(self):
        component = ScoreComponent.from_mapping(
            {
                "name": "living_area",
                "label": "Living Area",
                "weight": 24.0,
                "similarity": 0.917,
                "points": 22.0,
                "available": True,
            }
        )

        self.assertEqual(component.name, "living_area")
        self.assertEqual(component.label, "Living Area")
        self.assertEqual(component.weight, 24.0)
        self.assertEqual(component.similarity, 0.917)
        self.assertEqual(component.points, 22.0)

    def test_missing_similarity_and_points_default_to_none(self):
        # A factor neither property had data for: _component() in each
        # similarity module sets both to None when similarity is None.
        component = ScoreComponent.from_mapping(
            {
                "name": "stories",
                "label": "Stories",
                "weight": 4.0,
                "similarity": None,
                "points": None,
                "available": False,
            }
        )

        self.assertIsNone(component.similarity)
        self.assertIsNone(component.points)

    def test_ignores_extra_keys(self):
        # "available" is part of the raw dict but not part of ScoreComponent.
        component = ScoreComponent.from_mapping(
            {
                "name": "bedrooms",
                "label": "Bedrooms",
                "weight": 14.0,
                "similarity": 1.0,
                "points": 14.0,
                "available": True,
            }
        )

        self.assertFalse(hasattr(component, "available"))
