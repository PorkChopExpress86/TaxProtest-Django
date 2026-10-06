"""The shared similarity math, exercised through its public functions.

Expected values are worked by hand from the documented curve points and the
score-assembly rule, never recomputed the way the module does it.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from counties.common.similarity_math import (
    AGE_CURVE,
    BATHROOMS_CURVE,
    BEDROOMS_CURVE,
    LAND_SIZE_CURVE,
    LIVING_AREA_CURVE,
    STORIES_CURVE,
    categorical_similarity,
    clamp,
    component,
    difference_similarity,
    distance_similarity,
    get_similarity_label,
    interpolate_curve,
    normalized_code,
    percentage_similarity,
    ranked_code_similarity,
    safe_float,
    score_from_components,
)

RANKS = {"A": 6, "B": 5, "C": 4, "D": 3, "E": 2, "F": 1}


class TuningCurveTests(SimpleTestCase):
    def test_living_area_curve_scores_a_ten_percent_difference_at_its_point(self) -> None:
        self.assertEqual(percentage_similarity(2000, 2200, LIVING_AREA_CURVE), 0.78)

    def test_living_area_curve_interpolates_between_points(self) -> None:
        # 15% sits halfway between (0.10, 0.78) and (0.20, 0.55).
        self.assertAlmostEqual(percentage_similarity(2000, 1700, LIVING_AREA_CURVE), 0.665)

    def test_land_size_curve_reaches_zero_at_eighty_percent(self) -> None:
        self.assertEqual(percentage_similarity(10000, 2000, LAND_SIZE_CURVE), 0.0)
        self.assertEqual(percentage_similarity(10000, 9500, LAND_SIZE_CURVE), 0.90)

    def test_bedrooms_curve_scores_one_room_apart(self) -> None:
        self.assertEqual(difference_similarity(3, 4, BEDROOMS_CURVE), 0.62)

    def test_bathrooms_curve_scores_half_a_bath_apart(self) -> None:
        self.assertEqual(difference_similarity(2.0, 2.5, BATHROOMS_CURVE), 0.76)

    def test_age_curve_scores_ten_years_apart(self) -> None:
        self.assertEqual(difference_similarity(2000, 2010, AGE_CURVE), 0.42)

    def test_stories_curve_scores_one_story_apart(self) -> None:
        self.assertEqual(difference_similarity(1, 2, STORIES_CURVE), 0.35)


class NumericHelperTests(SimpleTestCase):
    def test_clamp_bounds_values(self) -> None:
        self.assertEqual(clamp(1.4), 1.0)
        self.assertEqual(clamp(-0.2), 0.0)
        self.assertEqual(clamp(150.0, lower=0.0, upper=100.0), 100.0)

    def test_safe_float_rejects_unconvertible_values(self) -> None:
        self.assertIsNone(safe_float(None))
        self.assertIsNone(safe_float("n/a"))
        self.assertEqual(safe_float("2.5"), 2.5)

    def test_normalized_code_strips_and_uppercases(self) -> None:
        self.assertEqual(normalized_code("  ab "), "AB")
        self.assertEqual(normalized_code(None), "")

    def test_interpolate_curve_holds_its_endpoints(self) -> None:
        curve = [(0.0, 1.0), (1.0, 0.5)]
        self.assertEqual(interpolate_curve(-3.0, curve), 1.0)
        self.assertEqual(interpolate_curve(9.0, curve), 0.5)
        self.assertEqual(interpolate_curve(0.5, curve), 0.75)
        self.assertEqual(interpolate_curve(0.5, []), 0.0)


class SimilarityFunctionTests(SimpleTestCase):
    def test_percentage_similarity_needs_a_positive_target(self) -> None:
        self.assertIsNone(percentage_similarity(0, 100, LIVING_AREA_CURVE))
        self.assertIsNone(percentage_similarity(100, None, LIVING_AREA_CURVE))

    def test_categorical_similarity_prefers_longer_prefixes(self) -> None:
        self.assertEqual(categorical_similarity("A1", "a1"), 1.0)
        self.assertEqual(categorical_similarity("A12", "A19"), 0.65)
        self.assertEqual(categorical_similarity("A1", "B1"), 0.0)
        self.assertEqual(categorical_similarity("A1", "A"), 0.4)
        self.assertIsNone(categorical_similarity("", "A1"))

    def test_ranked_code_similarity_follows_rank_distance(self) -> None:
        self.assertEqual(ranked_code_similarity("A", "A", RANKS), 1.0)
        self.assertEqual(ranked_code_similarity("A", "B", RANKS), 0.72)
        self.assertEqual(ranked_code_similarity("A", "C", RANKS), 0.42)
        self.assertAlmostEqual(ranked_code_similarity("A", "E", RANKS), 0.09)
        self.assertIsNone(ranked_code_similarity("A", "Z", RANKS))

    def test_distance_similarity_scales_by_search_radius(self) -> None:
        self.assertEqual(distance_similarity(0.0, 10.0), 1.0)
        self.assertEqual(distance_similarity(5.0, 10.0), 0.52)
        self.assertAlmostEqual(distance_similarity(25.0, 10.0), 0.05)
        self.assertIsNone(distance_similarity(1.0, 0.0))


class LabelBandTests(SimpleTestCase):
    def test_bands_start_at_their_thresholds(self) -> None:
        self.assertEqual(get_similarity_label(84), "Best match")
        self.assertEqual(get_similarity_label(83.9), "Highly similar")
        self.assertEqual(get_similarity_label(70), "Highly similar")
        self.assertEqual(get_similarity_label(69.9), "Good match")
        self.assertEqual(get_similarity_label(52), "Good match")
        self.assertEqual(get_similarity_label(51.9), "OK match")
        self.assertEqual(get_similarity_label(36), "OK match")
        self.assertEqual(get_similarity_label(35.9), "Broad match")


class ScoreAssemblyTests(SimpleTestCase):
    def test_component_uses_the_county_label_or_a_title_cased_name(self) -> None:
        labelled = component("building_character", 4.0, 0.5, labels={"building_character": "Type"})
        self.assertEqual(labelled["label"], "Type")
        self.assertEqual(component("land_size", 10.0, None)["label"], "Land Size")

    def test_component_rounds_similarity_and_points(self) -> None:
        self.assertEqual(
            component("age", 8.0, 0.123456),
            {
                "name": "age",
                "label": "Age",
                "weight": 8.0,
                "similarity": 0.123,
                "points": 1.0,
                "available": True,
            },
        )
        self.assertEqual(
            component("age", 8.0, None),
            {
                "name": "age",
                "label": "Age",
                "weight": 8.0,
                "similarity": None,
                "points": None,
                "available": False,
            },
        )

    def test_missing_components_scale_an_improved_score_down(self) -> None:
        components = [component("a", 10.0, 1.0), component("b", 30.0, None)]
        # Base 1.0; coverage 10/40 = 0.25; multiplier 0.8 + 0.2 * 0.25 = 0.85.
        result = score_from_components(components, is_land_only=False)
        self.assertEqual(result["score"], 85.0)
        self.assertEqual(result["available_weight"], 10.0)
        self.assertIs(result["components"], components)

    def test_land_only_scores_skip_the_completeness_multiplier(self) -> None:
        components = [component("a", 80.0, 0.5), component("b", 20.0, None)]
        self.assertEqual(score_from_components(components, is_land_only=True)["score"], 50.0)

    def test_no_available_components_scores_zero(self) -> None:
        components = [component("a", 10.0, None)]
        self.assertEqual(
            score_from_components(components, is_land_only=False),
            {"score": 0.0, "components": components, "available_weight": 0.0},
        )
