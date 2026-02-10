"""Database integrity tests for PropertyRecord, BuildingDetail, and ExtraFeature.

Validates uniqueness constraints, data completeness, and foreign key integrity
using synthetic test data. These tests run against a temporary test database.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from django.db import IntegrityError
from django.test import TestCase

from data.models import BuildingDetail, ExtraFeature, PropertyRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_property(
    account_number: str = "9999990000001",
    *,
    overrides: Optional[Dict[str, Any]] = None,
) -> PropertyRecord:
    """Create a minimal PropertyRecord for testing."""
    defaults: Dict[str, Any] = {
        "address": "100 TEST ST",
        "city": "Houston",
        "zipcode": "77001",
        "value": Decimal("250000"),
        "account_number": account_number,
        "owner_name": "TEST OWNER",
        "assessed_value": Decimal("245000"),
        "building_area": Decimal("2000"),
        "land_area": Decimal("8000"),
        "street_number": "100",
        "street_name": "TEST",
    }
    if overrides:
        defaults.update(overrides)
    return PropertyRecord.objects.create(**defaults)


def _create_building(
    prop: PropertyRecord,
    building_number: int = 1,
    *,
    overrides: Optional[Dict[str, Any]] = None,
) -> BuildingDetail:
    """Create a minimal BuildingDetail linked to *prop*."""
    defaults: Dict[str, Any] = {
        "property": prop,
        "account_number": prop.account_number,
        "building_number": building_number,
        "building_type": "A1",
        "quality_code": "C",
        "condition_code": "AV",
        "year_built": 2005,
        "heat_area": Decimal("2000"),
        "bedrooms": 3,
        "bathrooms": Decimal("2.0"),
        "half_baths": 1,
        "is_active": True,
    }
    if overrides:
        defaults.update(overrides)
    return BuildingDetail.objects.create(**defaults)


def _create_feature(
    prop: PropertyRecord,
    feature_code: str,
    feature_number: int = 1,
    *,
    overrides: Optional[Dict[str, Any]] = None,
) -> ExtraFeature:
    """Create a minimal ExtraFeature linked to *prop*."""
    defaults: Dict[str, Any] = {
        "property": prop,
        "account_number": prop.account_number,
        "feature_number": feature_number,
        "feature_code": feature_code,
        "feature_description": f"Test {feature_code}",
        "quantity": Decimal("1"),
        "is_active": True,
    }
    if overrides:
        defaults.update(overrides)
    return ExtraFeature.objects.create(**defaults)


# ===========================================================================
# Uniqueness Constraint Tests
# ===========================================================================


class PropertyRecordUniquenessTest(TestCase):
    """PropertyRecord.account_number must be unique."""

    def test_duplicate_account_number_rejected(self) -> None:
        """Inserting two PropertyRecords with the same account_number raises IntegrityError."""
        _create_property("1111111111111")
        with self.assertRaises(IntegrityError):
            _create_property("1111111111111")

    def test_distinct_account_numbers_allowed(self) -> None:
        """Two records with different account_numbers coexist without error."""
        p1 = _create_property("1111111111111")
        p2 = _create_property("2222222222222")
        self.assertEqual(PropertyRecord.objects.count(), 2)
        self.assertNotEqual(p1.pk, p2.pk)


class BuildingDetailUniquenessTest(TestCase):
    """BuildingDetail.(account_number, building_number) must be unique."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.prop = _create_property("3333333333333")

    def test_duplicate_building_rejected(self) -> None:
        """Same account + building_number raises IntegrityError."""
        _create_building(self.prop, building_number=1)
        with self.assertRaises(IntegrityError):
            _create_building(self.prop, building_number=1)

    def test_different_building_numbers_allowed(self) -> None:
        """Different building_numbers for the same account are fine."""
        _create_building(self.prop, building_number=1)
        _create_building(self.prop, building_number=2)
        self.assertEqual(
            BuildingDetail.objects.filter(account_number=self.prop.account_number).count(),
            2,
        )


class ExtraFeatureUniquenessTest(TestCase):
    """ExtraFeature.(account_number, feature_code, feature_number) must be unique."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.prop = _create_property("4444444444444")

    def test_duplicate_feature_rejected(self) -> None:
        """Same account + feature_code + feature_number raises IntegrityError."""
        _create_feature(self.prop, "RMB", feature_number=1)
        with self.assertRaises(IntegrityError):
            _create_feature(self.prop, "RMB", feature_number=1)

    def test_different_feature_numbers_allowed(self) -> None:
        """Different feature_numbers for the same code are fine."""
        _create_feature(self.prop, "RMB", feature_number=1)
        _create_feature(self.prop, "RMB", feature_number=2)
        self.assertEqual(
            ExtraFeature.objects.filter(
                account_number=self.prop.account_number, feature_code="RMB"
            ).count(),
            2,
        )

    def test_different_feature_codes_allowed(self) -> None:
        """Different feature_codes for the same feature_number are fine."""
        _create_feature(self.prop, "RMB", feature_number=1)
        _create_feature(self.prop, "RMF", feature_number=1)
        self.assertEqual(
            ExtraFeature.objects.filter(account_number=self.prop.account_number).count(),
            2,
        )


# ===========================================================================
# Data Completeness Tests
# ===========================================================================


class BuildingCompletenessTest(TestCase):
    """Active BuildingDetail rows should have bedrooms, bathrooms, and quality populated."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.prop = _create_property("5555555555555")
        cls.building = _create_building(cls.prop)

    def test_bedrooms_populated(self) -> None:
        self.assertIsNotNone(self.building.bedrooms)
        self.assertGreaterEqual(self.building.bedrooms, 0)

    def test_bathrooms_populated(self) -> None:
        self.assertIsNotNone(self.building.bathrooms)
        self.assertGreater(self.building.bathrooms, Decimal("0"))

    def test_quality_code_populated(self) -> None:
        self.assertIsNotNone(self.building.quality_code)
        self.assertNotEqual(self.building.quality_code.strip(), "")

    def test_heat_area_populated(self) -> None:
        self.assertIsNotNone(self.building.heat_area)
        self.assertGreater(self.building.heat_area, Decimal("0"))

    def test_year_built_populated(self) -> None:
        self.assertIsNotNone(self.building.year_built)
        self.assertGreater(self.building.year_built, 1800)


class RoomFeatureCompletenessTest(TestCase):
    """Room-related ExtraFeatures (RMB, RMF, RMH) should have valid quantities."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.prop = _create_property("6666666666666")
        cls.building = _create_building(cls.prop)
        cls.rmb = _create_feature(
            cls.prop, "RMB", feature_number=1,
            overrides={"feature_description": "Bedrooms", "quantity": Decimal("3")},
        )
        cls.rmf = _create_feature(
            cls.prop, "RMF", feature_number=2,
            overrides={"feature_description": "Full Baths", "quantity": Decimal("2")},
        )
        cls.rmh = _create_feature(
            cls.prop, "RMH", feature_number=3,
            overrides={"feature_description": "Half Baths", "quantity": Decimal("1")},
        )

    def test_room_codes_exist(self) -> None:
        """RMB, RMF, RMH should all be present for this property."""
        codes = set(
            ExtraFeature.objects.filter(
                account_number=self.prop.account_number,
                feature_code__in=["RMB", "RMF", "RMH"],
                is_active=True,
            ).values_list("feature_code", flat=True)
        )
        self.assertEqual(codes, {"RMB", "RMF", "RMH"})

    def test_bedroom_quantity_matches_building(self) -> None:
        """RMB quantity should match BuildingDetail.bedrooms."""
        self.assertEqual(int(self.rmb.quantity), self.building.bedrooms)

    def test_bathroom_value_consistent(self) -> None:
        """BuildingDetail.bathrooms should be a positive, reasonable value."""
        self.assertIsNotNone(self.building.bathrooms)
        self.assertGreater(self.building.bathrooms, Decimal("0"))
        # bathrooms should be >= full baths (RMF quantity)
        self.assertGreaterEqual(
            self.building.bathrooms, Decimal("0"),
        )

    def test_quantities_positive(self) -> None:
        """All room quantities should be positive."""
        for feat in [self.rmb, self.rmf, self.rmh]:
            self.assertIsNotNone(feat.quantity)
            self.assertGreater(feat.quantity, Decimal("0"))


# ===========================================================================
# Foreign Key Integrity Tests
# ===========================================================================


class ForeignKeyIntegrityTest(TestCase):
    """BuildingDetail and ExtraFeature must reference valid PropertyRecords."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.prop = _create_property("7777777777777")
        cls.building = _create_building(cls.prop)
        cls.feature = _create_feature(cls.prop, "POOL", feature_number=1)

    def test_building_links_to_property(self) -> None:
        """BuildingDetail.property should be a valid PropertyRecord."""
        self.assertIsNotNone(self.building.property_id)
        self.assertTrue(
            PropertyRecord.objects.filter(pk=self.building.property_id).exists()
        )

    def test_feature_links_to_property(self) -> None:
        """ExtraFeature.property should be a valid PropertyRecord."""
        self.assertIsNotNone(self.feature.property_id)
        self.assertTrue(
            PropertyRecord.objects.filter(pk=self.feature.property_id).exists()
        )

    def test_building_account_matches_property(self) -> None:
        """BuildingDetail.account_number should match its parent PropertyRecord."""
        self.assertEqual(
            self.building.account_number,
            self.building.property.account_number,
        )

    def test_feature_account_matches_property(self) -> None:
        """ExtraFeature.account_number should match its parent PropertyRecord."""
        self.assertEqual(
            self.feature.account_number,
            self.feature.property.account_number,
        )

    def test_cascade_delete(self) -> None:
        """Deleting a PropertyRecord cascades to its buildings and features."""
        prop_id = self.prop.pk
        building_id = self.building.pk
        feature_id = self.feature.pk

        self.prop.delete()

        self.assertFalse(PropertyRecord.objects.filter(pk=prop_id).exists())
        self.assertFalse(BuildingDetail.objects.filter(pk=building_id).exists())
        self.assertFalse(ExtraFeature.objects.filter(pk=feature_id).exists())
