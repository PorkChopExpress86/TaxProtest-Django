"""Regression tests for bedroom/bathroom data sourcing and rendering."""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.test import Client, TestCase

from counties.harris.etl_pipeline.fixtures_aggregator import update_building_room_counts
from counties.harris.models import BuildingDetail, PropertyRecord

TEST_ACCOUNT = "1074380000028"


def create_property_with_building(
    *,
    property_overrides: dict[str, Any] | None = None,
    building_overrides: dict[str, Any] | None = None,
) -> tuple[PropertyRecord, BuildingDetail]:
    """Create a PropertyRecord plus one BuildingDetail for test isolation."""

    property_defaults: dict[str, Any] = {
        "address": "16213 Wall St",
        "city": "Houston",
        "zipcode": "77040",
        "value": Decimal("325000"),
        "source_url": "https://example.com/property",
        "account_number": TEST_ACCOUNT,
        "owner_name": "DOE JOHN",
        "assessed_value": Decimal("315000"),
        "building_area": Decimal("2500"),
        "land_area": Decimal("9000"),
        "street_number": "16213",
        "street_name": "WALL",
    }
    if property_overrides:
        property_defaults.update(property_overrides)

    property_record = PropertyRecord.objects.create(**property_defaults)

    building_defaults: dict[str, Any] = {
        "property": property_record,
        "account_number": property_record.account_number,
        "building_number": 1,
        "building_type": "A1",
        "quality_code": "A",
        "heat_area": Decimal("2400"),
        "bedrooms": 4,
        "bathrooms": Decimal("2.5"),
        "half_baths": 1,
        "is_active": True,
    }
    if building_overrides:
        building_defaults.update(building_overrides)

    building = BuildingDetail.objects.create(**building_defaults)

    return property_record, building


class ETLLogicTest(TestCase):
    """Ensure fixture ETL updates BuildingDetail rows with room counts."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.property, cls.building = create_property_with_building(
            building_overrides={
                "bedrooms": None,
                "bathrooms": None,
                "half_baths": None,
            }
        )

    def test_load_fixtures_room_counts_updates_building(self) -> None:
        fixtures_content = "\n".join(
            [
                "acct\tbld_num\ttype\tunits",
                f"{TEST_ACCOUNT}\t1\tRMB\t4",
                f"{TEST_ACCOUNT}\t1\tRMF\t2",
                f"{TEST_ACCOUNT}\t1\tRMH\t1",
                "9999999999999\t1\tRMB\t2",
            ]
        )

        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp_file:
            tmp_file.write(fixtures_content)
            temp_path = tmp_file.name

        try:
            stats = update_building_room_counts(Path(temp_path), chunk_size=10)
        finally:
            os.unlink(temp_path)

        building = BuildingDetail.objects.get(pk=self.building.pk)
        self.assertEqual(building.bedrooms, 4)
        self.assertEqual(building.half_baths, 1)
        self.assertEqual(building.bathrooms, Decimal("2.5"))
        self.assertEqual(stats["buildings_updated"], 1)
        self.assertEqual(stats["buildings_not_found"], 1)

    def test_fixture_recovery_preserves_room_values_not_present_in_the_source(self) -> None:
        self.building.bedrooms = 2
        self.building.bathrooms = Decimal("2.5")
        self.building.half_baths = 1
        self.building.save(update_fields=["bedrooms", "bathrooms", "half_baths"])

        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp_file:
            tmp_file.write("acct\tbld_num\ttype\tunits\n")
            tmp_file.write(f"{TEST_ACCOUNT}\t1\tRMB\t4\n")
            temp_path = tmp_file.name

        try:
            update_building_room_counts(Path(temp_path), refresh_readiness=False)
        finally:
            os.unlink(temp_path)

        self.building.refresh_from_db()
        self.assertEqual(self.building.bedrooms, 4)
        self.assertEqual(self.building.bathrooms, Decimal("2.5"))
        self.assertEqual(self.building.half_baths, 1)


class ViewDisplayTest(TestCase):
    """Verify the index view renders bedroom/bathroom counts."""

    @classmethod
    def setUpTestData(cls) -> None:
        # Search lists only queryable records, so the property must be residential and data-ready.
        cls.property, cls.building = create_property_with_building(
            property_overrides={"is_residential": True, "is_data_ready": True}
        )

    def test_index_view_displays_bedrooms_bathrooms(self) -> None:
        client = Client()
        response = client.get(
            "/",
            {
                "address": "16213",
                "street_name": "Wall",
                "zip_code": "77040",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["filters_applied"])

        results = response.context["results"]
        self.assertEqual(len(results), 1)
        result = results[0]

        self.assertEqual(result["account_number"], TEST_ACCOUNT)
        self.assertEqual(result["bedrooms"], 4)
        self.assertAlmostEqual(float(result["bathrooms"]), 2.5)
