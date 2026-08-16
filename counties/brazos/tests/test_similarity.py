"""Tests for brazos_cad/similarity.py.

Mirrors the structure of data/tests/test_similarity_scoring.py but exercises
Brazos-specific logic: quality-tier extraction from class_code, the
SECOND-FLOOR stories proxy, multi-improvement primary-improvement selection,
and the combined quality+condition weight (see similarity.py's module
docstring for why condition isn't a separate component here).
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

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
    _primary_improvement,
    _quality_digit,
    _quality_similarity,
    calculate_similarity_details,
    find_similar_properties,
    get_similarity_label,
)
from counties.common.tax_models import ParcelGeometry

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

        improvement, characteristic = _primary_improvement("P1", TAX_YEAR)

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

        improvement, characteristic = _primary_improvement("P2", TAX_YEAR)

        self.assertEqual(improvement.imp_id, "I2")
        self.assertEqual(characteristic.bedrooms, 4)

    def test_no_improvements_returns_none_none(self):
        self.assertEqual(_primary_improvement("NOPE", TAX_YEAR), (None, None))


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


class GetSimilarityLabelTests(TestCase):
    def test_bands(self):
        self.assertEqual(get_similarity_label(90), "Best match")
        self.assertEqual(get_similarity_label(75), "Highly similar")
        self.assertEqual(get_similarity_label(60), "Good match")
        self.assertEqual(get_similarity_label(40), "OK match")
        self.assertEqual(get_similarity_label(10), "Broad match")


class FindSimilarPropertiesTests(TestCase):
    def test_finds_nearby_similar_property(self):
        target = PropertyAccount.objects.create(
            prop_id="000000010001",
            tax_year=TAX_YEAR,
            living_area=Decimal("2200"),
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
        ParcelGeometry.objects.create(
            account_number="000000010001",
            county="brazos",
            latitude=30.585,
            longitude=-96.298,
        )

        nearby = PropertyAccount.objects.create(
            prop_id="000000010002",
            tax_year=TAX_YEAR,
            living_area=Decimal("2150"),
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
        ParcelGeometry.objects.create(
            account_number="000000010002",
            county="brazos",
            latitude=30.586,
            longitude=-96.297,
        )

        # Far away -- outside the 10-mile default radius.
        PropertyAccount.objects.create(
            prop_id="000000099999",
            tax_year=TAX_YEAR,
            living_area=Decimal("2200"),
            class_code="RV3",
        )
        ParcelGeometry.objects.create(
            account_number="000000099999",
            county="brazos",
            latitude=31.000,
            longitude=-97.000,
        )

        results = find_similar_properties("000000010001")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["property"].prop_id, "000000010002")
        self.assertGreater(results[0]["similarity_score"], 70.0)

    def test_target_without_coordinates_returns_empty(self):
        PropertyAccount.objects.create(prop_id="000000010001", tax_year=TAX_YEAR)
        self.assertEqual(find_similar_properties("000000010001"), [])

    def test_unknown_prop_id_returns_empty(self):
        self.assertEqual(find_similar_properties("NOPE"), [])


class GeometryCandidateCapTests(TestCase):
    """ParcelGeometry rows with no PropertyAccount must not consume cap slots.

    ParcelGeometry is keyed by prop_id alone — it carries no tax_year and holds
    every parcel in the shapefile. Since the cap is applied to the geometry
    queryset, a parcel with no PropertyAccount row for the target's year has to
    be filtered out in SQL; otherwise it takes a slot and is then dropped at the
    join, costing a real comparable.
    """

    TARGET = "000000020001"
    STALE = "000000020002"
    COMP = "000000020003"

    def _account(self, prop_id: str, *, tax_year: int = TAX_YEAR) -> PropertyAccount:
        return PropertyAccount.objects.create(
            prop_id=prop_id,
            tax_year=tax_year,
            living_area=Decimal("2200"),
            class_code="RV3",
            year_built=2005,
        )

    def _geometry(self, prop_id: str, *, longitude: float) -> ParcelGeometry:
        return ParcelGeometry.objects.create(
            account_number=prop_id,
            county="brazos",
            latitude=30.585,
            longitude=longitude,
        )

    def test_geometry_without_an_account_this_year_does_not_crowd_out_a_comp(self):
        self._account(self.TARGET)
        self._geometry(self.TARGET, longitude=-96.298)
        PropertyLand.objects.create(
            prop_id=self.TARGET, tax_year=TAX_YEAR, land_seq=1, acreage=Decimal("0.25")
        )

        # Nearer than the real comp, but its only PropertyAccount row is for a
        # different year, so the target year's join will never find it.
        self._account(self.STALE, tax_year=TAX_YEAR - 1)
        self._geometry(self.STALE, longitude=-96.29801)

        self._account(self.COMP)
        self._geometry(self.COMP, longitude=-96.2981)
        PropertyLand.objects.create(
            prop_id=self.COMP, tax_year=TAX_YEAR, land_seq=1, acreage=Decimal("0.25")
        )

        with patch("counties.brazos.similarity.MAX_GEOMETRY_CANDIDATES", 1):
            results = find_similar_properties(self.TARGET, min_score=0.0)

        self.assertEqual([r["property"].prop_id for r in results], [self.COMP])
