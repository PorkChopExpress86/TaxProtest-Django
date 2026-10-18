"""Database integrity tests for PropertyRecord, BuildingDetail, and ExtraFeature.

Validates uniqueness constraints and cascade deletes using synthetic test data.
These tests run against a temporary test database.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import IntegrityError
from django.test import TestCase

from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_property(
    account_number: str = "9999990000001",
    *,
    overrides: dict[str, Any] | None = None,
) -> PropertyRecord:
    """Create a minimal PropertyRecord for testing."""
    defaults: dict[str, Any] = {
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
    overrides: dict[str, Any] | None = None,
) -> BuildingDetail:
    """Create a minimal BuildingDetail linked to *prop*."""
    defaults: dict[str, Any] = {
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
    overrides: dict[str, Any] | None = None,
) -> ExtraFeature:
    """Create a minimal ExtraFeature linked to *prop*."""
    defaults: dict[str, Any] = {
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
# Foreign Key Integrity Tests
# ===========================================================================


class ForeignKeyIntegrityTest(TestCase):
    """Deleting a PropertyRecord removes its BuildingDetail and ExtraFeature rows."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.prop = _create_property("7777777777777")
        cls.building = _create_building(cls.prop)
        cls.feature = _create_feature(cls.prop, "POOL", feature_number=1)

    def test_cascade_delete(self) -> None:
        """Deleting a PropertyRecord cascades to its buildings and features."""
        prop_id = self.prop.pk
        building_id = self.building.pk
        feature_id = self.feature.pk

        self.prop.delete()

        self.assertFalse(PropertyRecord.objects.filter(pk=prop_id).exists())
        self.assertFalse(BuildingDetail.objects.filter(pk=building_id).exists())
        self.assertFalse(ExtraFeature.objects.filter(pk=feature_id).exists())
