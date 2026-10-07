"""Brazos similarity, exercised through the public search.

Covers Brazos-specific scoring: the quality tier read from class_code, the
SECOND FLOOR stories proxy, building character, feature overlap,
multi-improvement primary-improvement selection, building-free scoring and
the combined quality+condition weight (see similarity.py's module docstring
for why condition isn't a separate component here).
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from counties.brazos.models import (
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyLand,
)
from counties.brazos.similarity import (
    RESIDENTIAL_WEIGHTS,
    find_similar_properties,
    primary_improvement,
)
from counties.common.tests.similarity_scenarios import (
    BRAZOS_TAX_YEAR,
    brazos_property,
    build_brazos_residential_scenario,
)

TAX_YEAR = 2025

SUBJECT = "BSUBJ0000001"
CANDIDATE = "BCAND0000001"


def _residential(prop_id: str, *, building: dict | None = None, second_floor=False, **fields):
    return brazos_property(
        prop_id,
        improvements=(
            {"year_built": 2005, "building": building or {}, "second_floor": second_floor},
        ),
        **fields,
    )


def _candidate_result() -> dict:
    results = find_similar_properties(SUBJECT, tax_year=BRAZOS_TAX_YEAR, min_score=0.0)
    return next(r for r in results if r["property"].prop_id == CANDIDATE)


def _components() -> dict[str, dict]:
    return {c["name"]: c for c in _candidate_result()["score_breakdown"]}


class ComponentScoringTests(TestCase):
    def test_identical_quality_digit_matches_across_class_prefixes(self):
        _residential(SUBJECT, class_code="RV3")
        _residential(CANDIDATE, lat_offset="0.001", class_code="RF3")
        self.assertEqual(_components()["quality"]["similarity"], 1.0)

    def test_distant_quality_digits_score_zero(self):
        # Eight tiers apart is past the rank curve's last point (5, 0.0).
        _residential(SUBJECT, class_code="RV1")
        _residential(CANDIDATE, lat_offset="0.001", class_code="RV9")
        self.assertEqual(_components()["quality"]["similarity"], 0.0)

    def test_class_code_without_a_digit_leaves_quality_unavailable(self):
        _residential(SUBJECT, class_code="RV3")
        _residential(CANDIDATE, lat_offset="0.001", class_code="AVERAGE")
        quality = _components()["quality"]
        self.assertFalse(quality["available"])
        self.assertIsNone(quality["similarity"])

    def test_building_character_scores_the_exterior_wall_first(self):
        # "BV" and "BR" share only their first character.
        _residential(SUBJECT, building={"exterior_wall": "BV", "construction_style": "FR"})
        _residential(
            CANDIDATE,
            lat_offset="0.001",
            building={"exterior_wall": "BR", "construction_style": "FR"},
        )
        self.assertEqual(_components()["building_character"]["similarity"], 0.4)

    def test_feature_overlap_is_shared_types_over_all_types(self):
        _residential(SUBJECT, features=("Fireplace", "Carport"))
        _residential(CANDIDATE, lat_offset="0.001", features=("Fireplace",))
        self.assertEqual(_components()["features"]["similarity"], 0.5)

    def test_second_floor_detail_row_counts_as_a_second_story(self):
        # One story apart sits on the stories curve's (1.0, 0.35) point.
        _residential(SUBJECT, second_floor=True)
        _residential(CANDIDATE, lat_offset="0.001")
        self.assertEqual(_components()["stories"]["similarity"], 0.35)

    def test_near_identical_properties_score_near_100(self):
        _residential(SUBJECT, acreage=("0.25",))
        _residential(CANDIDATE, lat_offset="0.001", acreage=("0.25",))
        self.assertGreaterEqual(_candidate_result()["similarity_score"], 95.0)

    def test_very_different_properties_score_low(self):
        _residential(
            SUBJECT,
            living_area=Decimal("4500"),
            class_code="RV9",
            building={"bedrooms": 6, "bathrooms": Decimal("5.0")},
            acreage=("50.0",),
        )
        _residential(
            CANDIDATE,
            lat_offset="0.13",
            living_area=Decimal("900"),
            class_code="RV1",
            building={"bedrooms": 1, "bathrooms": Decimal("1.0")},
            acreage=("2.0",),
        )
        self.assertLess(_candidate_result()["similarity_score"], 40.0)

    def test_neither_side_with_characteristics_scores_building_free(self):
        brazos_property(SUBJECT, acreage=("5.0",))
        brazos_property(CANDIDATE, lat_offset="0.001", acreage=("5.0",))
        self.assertEqual(
            [(c["name"], c["weight"]) for c in _candidate_result()["score_breakdown"]],
            [("land_size", 80.0), ("features", 10.0), ("distance", 10.0)],
        )

    def test_component_weights_sum_to_100(self):
        self.assertEqual(sum(RESIDENTIAL_WEIGHTS.values()), 100.0)


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
        brazos_property(
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
        brazos_property(
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
