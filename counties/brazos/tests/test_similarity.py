"""Tests for brazos_cad/similarity.py.

Mirrors the structure of data/tests/test_similarity_scoring.py but exercises
Brazos-specific logic: quality-tier extraction from class_code, the
SECOND-FLOOR stories proxy, multi-improvement primary-improvement selection,
and the combined quality+condition weight (see similarity.py's module
docstring for why condition isn't a separate component here).
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from counties.brazos.models import (
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyLand,
)
from counties.brazos.similarity import (
    RESIDENTIAL_WEIGHTS,
    _building_character_similarity,
    _feature_similarity,
    _has_second_floor,
    _quality_digit,
    _quality_similarity,
    calculate_similarity_details,
    find_similar_properties,
    load_scoring_facts,
    primary_improvement,
    score_pair,
)
from counties.common.similarity_math import nearby_properties
from counties.common.tests.similarity_scenarios import (
    BRAZOS_TAX_YEAR,
    _brazos_property,
    build_brazos_building_free_scenario,
    build_brazos_residential_scenario,
)

TAX_YEAR = 2025


class QualitySimilarityTests(TestCase):
    def test_extracts_digit_from_class_code(self):
        self.assertEqual(_quality_digit("RV3"), 3)
        self.assertEqual(_quality_digit("RF4P"), 4)
        self.assertIsNone(_quality_digit(""))
        self.assertIsNone(_quality_digit("AVERAGE"))

    def test_identical_digit_is_perfect_match(self):
        self.assertEqual(_quality_similarity("RV3", "RF3"), 1.0)

    def test_distant_digit_is_low_similarity(self):
        score = _quality_similarity("RV1", "RV9")
        self.assertLess(score, 0.1)

    def test_missing_class_code_returns_none(self):
        self.assertIsNone(_quality_similarity("", "RV3"))


class BuildingCharacterSimilarityTests(TestCase):
    def test_matches_on_exterior_wall(self):
        target = PropertyBuildingCharacteristic(
            exterior_wall="BV", construction_style="", foundation=""
        )
        candidate = PropertyBuildingCharacteristic(
            exterior_wall="BV", construction_style="FR", foundation="CS"
        )
        self.assertEqual(_building_character_similarity(target, candidate), 1.0)

    def test_none_building_returns_none(self):
        self.assertIsNone(_building_character_similarity(None, None))


class FeatureSimilarityTests(TestCase):
    def test_jaccard_overlap(self):
        target = [
            PropertyExtraFeature(feature_type="Fireplace"),
            PropertyExtraFeature(feature_type="Carport"),
        ]
        candidate = [PropertyExtraFeature(feature_type="Fireplace")]
        # intersection=1, union=2 -> 0.5
        self.assertEqual(_feature_similarity(target, candidate), 0.5)

    def test_both_empty_returns_none(self):
        self.assertIsNone(_feature_similarity([], []))


class PrimaryImprovementSelectionTests(TestCase):
    def test_prefers_residential_type_with_characteristics(self):
        PropertyImprovement.objects.create(
            prop_id="P1", imp_id="I1", tax_year=TAX_YEAR, improvement_type="M"
        )
        PropertyImprovement.objects.create(
            prop_id="P1", imp_id="I2", tax_year=TAX_YEAR, improvement_type="R"
        )
        PropertyBuildingCharacteristic.objects.create(
            prop_id="P1", imp_id="I2", tax_year=TAX_YEAR, bedrooms=3
        )

        improvement, characteristic = primary_improvement("P1", TAX_YEAR)

        self.assertEqual(improvement.imp_id, "I2")
        self.assertEqual(characteristic.bedrooms, 3)

    def test_multiple_residential_improvements_first_with_characteristics_wins(self):
        PropertyImprovement.objects.create(
            prop_id="P2", imp_id="I1", tax_year=TAX_YEAR, improvement_type="R"
        )
        PropertyImprovement.objects.create(
            prop_id="P2", imp_id="I2", tax_year=TAX_YEAR, improvement_type="R"
        )
        # Only I2 has characteristics -- I1 has none (e.g. a detached garage
        # with no bedroom/bathroom attributes at all).
        PropertyBuildingCharacteristic.objects.create(
            prop_id="P2", imp_id="I2", tax_year=TAX_YEAR, bedrooms=4
        )

        improvement, characteristic = primary_improvement("P2", TAX_YEAR)

        self.assertEqual(improvement.imp_id, "I2")
        self.assertEqual(characteristic.bedrooms, 4)

    def test_no_improvements_returns_none_none(self):
        self.assertEqual(primary_improvement("NOPE", TAX_YEAR), (None, None))


class SimilarityPrivateNameBoundaryTests(SimpleTestCase):
    def test_no_production_module_imports_a_private_similarity_name(self):
        root = Path(__file__).resolve().parents[3]
        offenders = []
        for package in ("counties", "taxprotest"):
            for path in (root / package).rglob("*.py"):
                if "tests" in path.parts or path.name.startswith("test_"):
                    continue
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if (
                        isinstance(node, ast.ImportFrom)
                        and node.module == "counties.brazos.similarity"
                    ):
                        offenders.extend(
                            f"{path.relative_to(root)}: {alias.name}"
                            for alias in node.names
                            if alias.name.startswith("_")
                        )
        self.assertEqual(offenders, [])


class SecondFloorStoriesTests(TestCase):
    def test_second_floor_detail_row_detected(self):
        PropertyImprovementDetail.objects.create(
            prop_id="P1",
            imp_id="I1",
            tax_year=TAX_YEAR,
            detail_seq=1,
            detail_description="MAIN AREA",
        )
        PropertyImprovementDetail.objects.create(
            prop_id="P1",
            imp_id="I1",
            tax_year=TAX_YEAR,
            detail_seq=2,
            detail_description="SECOND FLOOR",
        )
        self.assertTrue(_has_second_floor("P1", "I1", TAX_YEAR))

    def test_no_second_floor_row_is_false(self):
        PropertyImprovementDetail.objects.create(
            prop_id="P1",
            imp_id="I1",
            tax_year=TAX_YEAR,
            detail_seq=1,
            detail_description="MAIN AREA",
        )
        self.assertFalse(_has_second_floor("P1", "I1", TAX_YEAR))


class CalculateSimilarityDetailsTests(TestCase):
    def _account(self, prop_id: str, **overrides) -> PropertyAccount:
        defaults = {
            "prop_id": prop_id,
            "tax_year": TAX_YEAR,
            "living_area": Decimal("2200"),
            "latitude": Decimal("30.6700000"),
            "longitude": Decimal("-96.3700000"),
            "class_code": "RV3",
            "year_built": 2005,
        }
        defaults.update(overrides)
        return PropertyAccount.objects.create(**defaults)

    def _building(self, prop_id: str, imp_id: str, **overrides) -> PropertyBuildingCharacteristic:
        defaults = {
            "prop_id": prop_id,
            "imp_id": imp_id,
            "tax_year": TAX_YEAR,
            "bedrooms": 4,
            "bathrooms": Decimal("2.5"),
            "exterior_wall": "BV",
        }
        defaults.update(overrides)
        return PropertyBuildingCharacteristic.objects.create(**defaults)

    def test_identical_properties_score_near_100(self):
        target_account = self._account("P1")
        target_building = self._building("P1", "I1")
        candidate_account = self._account("P2", class_code="RV3", year_built=2005)
        candidate_building = self._building("P2", "I1", bedrooms=4, bathrooms=Decimal("2.5"))

        details = calculate_similarity_details(
            target_account,
            candidate_account,
            None,
            None,
            target_building,
            candidate_building,
            [],
            [],
            10.0,
            10.0,
            distance=0.1,
        )

        self.assertGreaterEqual(details["score"], 95.0)

    def test_very_different_properties_score_low(self):
        target_account = self._account("P1", living_area=Decimal("4500"), class_code="RV9")
        target_building = self._building("P1", "I1", bedrooms=6, bathrooms=Decimal("5.0"))
        candidate_account = self._account("P2", living_area=Decimal("900"), class_code="RV1")
        candidate_building = self._building("P2", "I1", bedrooms=1, bathrooms=Decimal("1.0"))

        details = calculate_similarity_details(
            target_account,
            candidate_account,
            None,
            None,
            target_building,
            candidate_building,
            [],
            [],
            50.0,
            2.0,
            distance=9.5,
        )

        self.assertLess(details["score"], 40.0)

    def test_missing_building_falls_back_to_land_only_weights(self):
        target_account = self._account("P1")
        candidate_account = self._account("P2")

        details = calculate_similarity_details(
            target_account,
            candidate_account,
            None,
            None,
            None,
            None,
            [],
            [],
            5.0,
            5.0,
            distance=0.0,
        )

        component_names = {c["name"] for c in details["components"]}
        self.assertEqual(component_names, {"land_size", "features", "distance"})

    def test_component_weights_sum_to_100(self):
        self.assertEqual(sum(RESIDENTIAL_WEIGHTS.values()), 100.0)


class FindSimilarPropertiesTests(TestCase):
    def test_finds_nearby_similar_property(self):
        target = PropertyAccount.objects.create(
            prop_id="000000010001",
            tax_year=TAX_YEAR,
            living_area=Decimal("2200"),
            latitude=Decimal("30.6700000"),
            longitude=Decimal("-96.3700000"),
            class_code="RV3",
            year_built=2005,
        )
        PropertyImprovement.objects.create(
            prop_id="000000010001", imp_id="I1", tax_year=TAX_YEAR, improvement_type="R"
        )
        PropertyBuildingCharacteristic.objects.create(
            prop_id="000000010001",
            imp_id="I1",
            tax_year=TAX_YEAR,
            bedrooms=4,
            bathrooms=Decimal("2.5"),
        )
        PropertyLand.objects.create(
            prop_id="000000010001", tax_year=TAX_YEAR, land_seq=1, acreage=Decimal("0.25")
        )

        nearby = PropertyAccount.objects.create(
            prop_id="000000010002",
            tax_year=TAX_YEAR,
            living_area=Decimal("2150"),
            latitude=Decimal("30.6710000"),
            longitude=Decimal("-96.3710000"),
            class_code="RV3",
            year_built=2004,
        )
        PropertyImprovement.objects.create(
            prop_id="000000010002", imp_id="I1", tax_year=TAX_YEAR, improvement_type="R"
        )
        PropertyBuildingCharacteristic.objects.create(
            prop_id="000000010002",
            imp_id="I1",
            tax_year=TAX_YEAR,
            bedrooms=4,
            bathrooms=Decimal("2.0"),
        )
        PropertyLand.objects.create(
            prop_id="000000010002", tax_year=TAX_YEAR, land_seq=1, acreage=Decimal("0.27")
        )

        # Far away -- outside the 10-mile default radius.
        PropertyAccount.objects.create(
            prop_id="000000099999",
            tax_year=TAX_YEAR,
            living_area=Decimal("2200"),
            latitude=Decimal("31.5000000"),
            longitude=Decimal("-97.5000000"),
            class_code="RV3",
        )

        results = find_similar_properties("000000010001", tax_year=TAX_YEAR)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["property"].prop_id, "000000010002")
        self.assertGreater(results[0]["similarity_score"], 70.0)

    def test_candidate_does_not_inherit_another_propertys_building_facts(self):
        # Brazos improvement identifiers repeat across properties, so a shared
        # imp_id must never carry one property's characteristics onto another.
        shared_imp_id = "SHARED-1"
        for prop_id, lat, building in (
            ("000000020001", "30.6700000", True),
            ("000000020002", "30.6705000", True),
            ("000000020003", "30.6710000", False),
        ):
            PropertyAccount.objects.create(
                prop_id=prop_id,
                tax_year=TAX_YEAR,
                living_area=Decimal("2200"),
                latitude=Decimal(lat),
                longitude=Decimal("-96.3700000"),
                class_code="RV3",
                year_built=2005,
            )
            PropertyImprovement.objects.create(
                prop_id=prop_id, imp_id=shared_imp_id, tax_year=TAX_YEAR, improvement_type="R"
            )
            if building:
                PropertyBuildingCharacteristic.objects.create(
                    prop_id=prop_id,
                    imp_id=shared_imp_id,
                    tax_year=TAX_YEAR,
                    bedrooms=4,
                    bathrooms=Decimal("2.5"),
                    exterior_wall="BV",
                )

        results = find_similar_properties("000000020001", tax_year=TAX_YEAR)

        by_prop = {r["property"].prop_id: r for r in results}
        self.assertEqual(set(by_prop), {"000000020002", "000000020003"})
        self.assertEqual(by_prop["000000020002"]["building"].prop_id, "000000020002")
        self.assertIsNone(by_prop["000000020003"]["building"])

    def test_target_without_coordinates_returns_empty(self):
        PropertyAccount.objects.create(prop_id="000000010001", tax_year=TAX_YEAR)
        self.assertEqual(find_similar_properties("000000010001", tax_year=TAX_YEAR), [])

    def test_unknown_prop_id_returns_empty(self):
        self.assertEqual(find_similar_properties("NOPE", tax_year=TAX_YEAR), [])


class NearbyPropertiesCapTests(TestCase):
    """The shared nearest-properties query caps Brazos candidates nearest-first."""

    def _account(self, prop_id: str, lat_offset: str, tax_year: int = TAX_YEAR) -> None:
        PropertyAccount.objects.create(
            prop_id=prop_id,
            tax_year=tax_year,
            living_area=Decimal("2200"),
            latitude=Decimal("30.6700000") + Decimal(lat_offset),
            longitude=Decimal("-96.3700000"),
            class_code="RV3",
            year_built=2005,
        )

    def setUp(self):
        self._account("BCAP00000000", "0")
        # Nearest of all, but another tax year: removed by Brazos's pre-filter before the cap.
        self._account("BCAP00000001", "0.001", tax_year=TAX_YEAR - 1)
        self._account("BCAP00000002", "0.002")
        self._account("BCAP00000003", "0.003")
        self._account("BCAP00000004", "0.004")

    def _found(self) -> list[str]:
        results = find_similar_properties(
            "BCAP00000000", tax_year=TAX_YEAR, max_results=50, min_score=0.0
        )
        return sorted(result["property"].prop_id for result in results)

    def test_search_scores_only_the_nearest_capped_candidates_after_pre_filters(self):
        with mock.patch("counties.common.similarity_math.NEARBY_PROPERTIES_CAP", 2):
            self.assertEqual(self._found(), ["BCAP00000002", "BCAP00000003"])

    def test_default_cap_admits_every_nearby_candidate(self):
        self.assertEqual(self._found(), ["BCAP00000002", "BCAP00000003", "BCAP00000004"])


class PairScoringQueryCountTests(TestCase):
    """A search's query total does not grow with the number of candidates."""

    def _candidate(self, prefix: str, index: int) -> None:
        _brazos_property(
            f"{prefix}{index:010d}",
            lat_offset=f"0.00{index % 9 + 1}",
            improvements=(
                {"improvement_type": "M"},
                {"year_built": 1999},
                {"year_built": 2001, "building": {}, "second_floor": index % 2 == 0},
            ),
            acreage=("0.20", "0.05"),
            features=("Fireplace",),
        )

    def _search_query_count(self, prefix: str, candidate_count: int) -> int:
        _brazos_property(
            f"{prefix}0000000000",
            improvements=({"year_built": 2006, "building": {}, "second_floor": True},),
            acreage=("0.25",),
            features=("Fireplace", "Carport"),
        )
        for index in range(1, candidate_count + 1):
            self._candidate(prefix, index)
        with CaptureQueriesContext(connection) as queries:
            results = find_similar_properties(f"{prefix}0000000000", tax_year=BRAZOS_TAX_YEAR)
        self.assertEqual(len(results), candidate_count)
        return len(queries)

    def test_small_and_large_candidate_sets_issue_equal_query_totals(self):
        small = self._search_query_count("BS", 2)
        PropertyAccount.objects.filter(prop_id__startswith="BS").delete()
        large = self._search_query_count("BL", 12)
        self.assertEqual(small, large)


class PurePairScorerDifferentialTests(TestCase):
    """Temporary: the pure scorer equals the old per-pair scorer across the golden
    matrix. Issue #88 deletes this test together with calculate_similarity_details."""

    def _old_inputs(self, prop_id: str) -> tuple:
        improvement, building = primary_improvement(prop_id, BRAZOS_TAX_YEAR)
        features = list(
            PropertyExtraFeature.objects.filter(prop_id=prop_id, tax_year=BRAZOS_TAX_YEAR)
        )
        acreages = [
            float(a)
            for a in PropertyLand.objects.filter(
                prop_id=prop_id, tax_year=BRAZOS_TAX_YEAR
            ).values_list("acreage", flat=True)
            if a is not None
        ]
        return improvement, building, features, (sum(acreages) if acreages else None)

    def _assert_scorers_agree_for_every_target(self) -> int:
        compared = 0
        targets = PropertyAccount.objects.filter(tax_year=BRAZOS_TAX_YEAR).exclude(
            latitude__isnull=True
        )
        for target in targets:
            candidates = list(
                nearby_properties(
                    PropertyAccount.objects.filter(tax_year=BRAZOS_TAX_YEAR).exclude(
                        prop_id=target.prop_id
                    ),
                    latitude=float(target.latitude),
                    longitude=float(target.longitude),
                    max_distance_miles=10.0,
                )
            )
            facts = load_scoring_facts([target, *candidates], BRAZOS_TAX_YEAR)
            t_imp, t_bld, t_feat, t_acre = self._old_inputs(target.prop_id)
            for candidate in candidates:
                c_imp, c_bld, c_feat, c_acre = self._old_inputs(candidate.prop_id)
                old = calculate_similarity_details(
                    target,
                    candidate,
                    t_imp,
                    c_imp,
                    t_bld,
                    c_bld,
                    t_feat,
                    c_feat,
                    t_acre,
                    c_acre,
                    candidate.distance,
                    max_distance_miles=10.0,
                )
                with self.assertNumQueries(0):
                    new = score_pair(
                        facts[target.prop_id],
                        facts[candidate.prop_id],
                        distance=candidate.distance,
                        max_distance_miles=10.0,
                    )
                self.assertEqual(new, old, (target.prop_id, candidate.prop_id))
                compared += 1
        return compared

    def test_residential_matrix_scores_identically(self):
        build_brazos_residential_scenario()
        self.assertGreater(self._assert_scorers_agree_for_every_target(), 40)

    def test_building_free_matrix_scores_identically(self):
        build_brazos_building_free_scenario()
        self.assertEqual(self._assert_scorers_agree_for_every_target(), 12)


class PairScoringReadsCurrentFactsTests(TestCase):
    """The scorer keeps no state between searches: a changed fact changes the score."""

    def test_adding_a_second_floor_changes_the_next_search(self):
        subject = build_brazos_residential_scenario()

        def stories_row(results: list[dict]) -> tuple:
            row = next(r for r in results if r["property"].prop_id == "BC0000000002")
            stories = next(c for c in row["score_breakdown"] if c["name"] == "stories")
            return row["similarity_score"], stories["similarity"]

        before = stories_row(find_similar_properties(subject, tax_year=BRAZOS_TAX_YEAR))
        PropertyImprovementDetail.objects.create(
            prop_id="BC0000000002",
            imp_id="IMP-BC0000000002-1",
            tax_year=BRAZOS_TAX_YEAR,
            detail_seq=2,
            detail_description="SECOND FLOOR",
        )
        after = stories_row(find_similar_properties(subject, tax_year=BRAZOS_TAX_YEAR))

        self.assertLess(before[1], after[1])
        self.assertLess(before[0], after[0])
