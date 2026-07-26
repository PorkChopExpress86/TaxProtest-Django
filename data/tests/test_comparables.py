"""Tests for the county-neutral comparable adapter."""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from data.comparables import (
    BrazosSource,
    ComparableProperty,
    HcadSource,
    brazos_comparable,
    get_source,
    hcad_comparable,
    resolve_source,
)
from data.models import (
    BuildingDetail,
    ExtraFeature,
    PropertyAccount,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyRecord,
)
from data.similarity import find_similar_properties, score_comparables


class HcadMappingTests(TestCase):
    def setUp(self):
        self.record = PropertyRecord.objects.create(
            address="123 MAIN ST",
            account_number="ACCT1",
            land_area=Decimal("7000"),
            latitude=Decimal("29.7604"),
            longitude=Decimal("-95.3698"),
        )
        self.building = BuildingDetail.objects.create(
            property=self.record,
            account_number="ACCT1",
            building_number=1,
            heat_area=Decimal("2000"),
            bedrooms=3,
            bathrooms=Decimal("2.0"),
            quality_code="B",
            condition_code="A",
            stories=Decimal("1.0"),
            year_built=1995,
            building_style="R1",
        )

    def test_maps_building_attributes(self):
        comparable = hcad_comparable(self.record, self.building, [])
        self.assertEqual(comparable.key, "ACCT1")
        self.assertEqual(comparable.living_area, 2000.0)
        self.assertEqual(comparable.bedrooms, 3.0)
        self.assertEqual(comparable.bathrooms, 2.0)
        self.assertEqual(comparable.quality_code, "B")
        self.assertEqual(comparable.effective_year, 1995)
        self.assertTrue(comparable.has_building)
        self.assertTrue(comparable.has_location)

    def test_effective_year_prefers_most_specific(self):
        self.building.year_remodeled = 2010
        self.building.effective_year = 2015
        self.assertEqual(hcad_comparable(self.record, self.building, []).effective_year, 2015)
        self.building.effective_year = None
        self.assertEqual(hcad_comparable(self.record, self.building, []).effective_year, 2010)

    def test_land_only_when_no_building(self):
        comparable = hcad_comparable(self.record, None, [])
        self.assertFalse(comparable.has_building)
        self.assertIsNone(comparable.living_area)

    def test_keeps_raw_rows_for_existing_callers(self):
        feature = ExtraFeature.objects.create(
            property=self.record, account_number="ACCT1", feature_code="POOL", feature_number=1
        )
        comparable = hcad_comparable(self.record, self.building, [feature])
        self.assertIs(comparable.building, self.building)
        self.assertEqual(list(comparable.features), [feature])
        self.assertEqual(comparable.feature_codes, frozenset({"POOL"}))


class BrazosMappingTests(TestCase):
    def setUp(self):
        self.account = PropertyAccount.objects.create(
            prop_id=1001,
            tax_year=2025,
            prop_type_cd="R",
            land_acres=Decimal("0.2000"),
            latitude=Decimal("30.6280"),
            longitude=Decimal("-96.3344"),
            imprv_state_cd="A1",
        )

    def detail(self, code, area, **kwargs):
        return PropertyImprovementDetail.objects.create(
            prop_id=1001,
            tax_year=2025,
            imp_id=kwargs.pop("imp_id", 1),
            imprv_det_id=kwargs.pop("det_id", abs(hash(code)) % 100000),
            imprv_det_type_cd=code,
            imprv_det_area=Decimal(str(area)),
            **kwargs,
        )

    def test_living_area_sums_main_area_rows_only(self):
        # MA + MA2 are heated living space; a garage and porch are not.
        self.detail("MA", 1500, imprv_det_class_cd="RV4", yr_built=2010)
        self.detail("MA2", 800)
        self.detail("AG", 500)
        self.detail("OP", 120)
        comparable = brazos_comparable(self.account, list(PropertyImprovementDetail.objects.all()))
        self.assertEqual(comparable.living_area, 2300.0)

    def test_non_living_rows_become_features(self):
        self.detail("MA", 1500)
        self.detail("AG", 500)
        self.detail("SP", 1)
        comparable = brazos_comparable(self.account, list(PropertyImprovementDetail.objects.all()))
        self.assertEqual(comparable.feature_codes, frozenset({"AG", "SP"}))
        self.assertNotIn("MA", comparable.feature_codes)

    def test_quality_and_year_come_from_the_largest_dwelling(self):
        # A parcel can hold two houses; the primary one sets the attributes.
        self.detail("MA", 900, imprv_det_class_cd="RF2", yr_built=1960, det_id=1)
        self.detail("MA", 2400, imprv_det_class_cd="RV5", yr_built=2018, det_id=2)
        comparable = brazos_comparable(self.account, list(PropertyImprovementDetail.objects.all()))
        self.assertEqual(comparable.quality_code, "RV5")
        self.assertEqual(comparable.effective_year, 2018)

    def test_second_floor_row_implies_two_storeys(self):
        self.detail("MA", 1500)
        self.detail("MA2", 700)
        comparable = brazos_comparable(self.account, list(PropertyImprovementDetail.objects.all()))
        self.assertEqual(comparable.stories, 2.0)

    def test_single_storey_when_only_main_area(self):
        self.detail("MA", 1500)
        comparable = brazos_comparable(self.account, list(PropertyImprovementDetail.objects.all()))
        self.assertEqual(comparable.stories, 1.0)

    def test_unpublished_factors_are_none_not_zero(self):
        # Zero would read as "0 bedrooms" and skew scoring; None is skipped.
        self.detail("MA", 1500)
        comparable = brazos_comparable(self.account, list(PropertyImprovementDetail.objects.all()))
        self.assertIsNone(comparable.bedrooms)
        self.assertIsNone(comparable.bathrooms)
        self.assertEqual(comparable.condition_code, "")

    def test_land_area_converts_acres_to_square_feet(self):
        comparable = brazos_comparable(self.account, [])
        self.assertAlmostEqual(comparable.land_area, 0.2 * 43560, places=2)

    def test_land_area_falls_back_to_parcel_geometry(self):
        self.account.land_acres = None
        self.account.parcel_area_sqft = Decimal("8000")
        self.assertEqual(brazos_comparable(self.account, []).land_area, 8000.0)

    def test_character_prefers_improvement_type(self):
        improvement = PropertyImprovement.objects.create(
            prop_id=1001, tax_year=2025, imp_id=1, imprv_type_cd="R"
        )
        comparable = brazos_comparable(self.account, [], [improvement])
        self.assertEqual(comparable.character_codes, ("R",))

    def test_account_with_no_improvements_is_land_only(self):
        comparable = brazos_comparable(self.account, [])
        self.assertFalse(comparable.has_building)


class SourceResolutionTests(TestCase):
    def setUp(self):
        PropertyRecord.objects.create(
            address="1 A ST",
            account_number="12345",
            latitude=Decimal("29.76"),
            longitude=Decimal("-95.37"),
        )
        PropertyAccount.objects.create(
            prop_id=999, tax_year=2025, latitude=Decimal("30.62"), longitude=Decimal("-96.33")
        )

    def test_get_source_by_name(self):
        self.assertIsInstance(get_source("hcad"), HcadSource)
        self.assertIsInstance(get_source("brazos"), BrazosSource)

    def test_unknown_source_raises(self):
        with self.assertRaisesRegex(ValueError, "Unknown comparable source"):
            get_source("travis")

    def test_auto_detect_finds_each_county(self):
        hcad, _ = resolve_source("12345")
        self.assertEqual(hcad.name, "hcad")
        brazos, _ = resolve_source("999")
        self.assertEqual(brazos.name, "brazos")

    def test_explicit_source_wins(self):
        # "12345" exists as an HCAD account but not as a Brazos prop_id.
        self.assertIsNone(resolve_source("12345", "brazos"))

    def test_unknown_key_resolves_to_nothing(self):
        self.assertIsNone(resolve_source("does-not-exist"))

    def test_brazos_uses_latest_year_when_unspecified(self):
        PropertyAccount.objects.create(
            prop_id=999, tax_year=2024, latitude=Decimal("30.62"), longitude=Decimal("-96.33")
        )
        source = BrazosSource()
        source.get_target("999")
        self.assertEqual(source.tax_year, 2025)


class CrossCountyScoringTests(TestCase):
    """The scorer works on the neutral shape, with no model dependency."""

    def comparable(self, **kwargs) -> ComparableProperty:
        base = {
            "key": "x",
            "source": None,
            "living_area": 2000.0,
            "land_area": 8000.0,
            "quality_code": "RV4",
            "effective_year": 2010,
            "stories": 1.0,
            "has_building": True,
        }
        return ComparableProperty(**{**base, **kwargs})

    def test_identical_properties_score_highly(self):
        a = self.comparable(key="a")
        b = self.comparable(key="b")
        self.assertGreater(float(score_comparables(a, b, 0.0, 10.0)["score"]), 84)

    def test_missing_factors_are_skipped_not_zeroed(self):
        a = self.comparable(key="a", bedrooms=None)
        b = self.comparable(key="b", bedrooms=None)
        breakdown = score_comparables(a, b, 0.0, 10.0)["components"]
        bedrooms = next(c for c in breakdown if c["name"] == "bedrooms")
        self.assertFalse(bedrooms["available"])
        self.assertIsNone(bedrooms["similarity"])

    def test_absent_room_counts_do_not_collapse_the_score(self):
        # Brazos has no room counts; scores must stay usable without them.
        with_rooms = score_comparables(
            self.comparable(key="a", bedrooms=3.0, bathrooms=2.0),
            self.comparable(key="b", bedrooms=3.0, bathrooms=2.0),
            0.0,
            10.0,
        )
        without = score_comparables(self.comparable(key="a"), self.comparable(key="b"), 0.0, 10.0)
        self.assertGreater(float(without["score"]), 80)
        # The completeness penalty means fewer factors scores slightly lower.
        self.assertLess(float(without["score"]), float(with_rooms["score"]))

    def test_brazos_quality_codes_compare_by_grade(self):
        near = score_comparables(
            self.comparable(key="a", quality_code="RV4"),
            self.comparable(key="b", quality_code="RV3"),
            0.0,
            10.0,
        )
        far = score_comparables(
            self.comparable(key="a", quality_code="RV4"),
            self.comparable(key="b", quality_code="RV1"),
            0.0,
            10.0,
        )
        self.assertGreater(float(near["score"]), float(far["score"]))

    def test_land_only_pair_uses_land_weights(self):
        a = self.comparable(key="a", has_building=False, living_area=None)
        b = self.comparable(key="b", has_building=False, living_area=None)
        names = {c["name"] for c in score_comparables(a, b, 0.0, 10.0)["components"]}
        self.assertEqual(names, {"land_size", "features", "distance"})


class FindSimilarSourceTests(TestCase):
    def test_missing_account_returns_empty(self):
        self.assertEqual(find_similar_properties("nope"), [])

    def test_target_without_coordinates_returns_empty(self):
        PropertyAccount.objects.create(prop_id=4242, tax_year=2025)
        self.assertEqual(find_similar_properties("4242", source="brazos"), [])

    def test_unknown_source_name_raises(self):
        with self.assertRaises(ValueError):
            find_similar_properties("1", source="travis")
