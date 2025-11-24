"""Regression tests for bedroom/bathroom data sourcing and rendering."""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from typing import Tuple, Optional, Dict, Any

from django.test import Client, TestCase

from data.etl import load_fixtures_room_counts
from data.models import BuildingDetail, ExtraFeature, PropertyRecord


TEST_ACCOUNT = "1074380000028"


def create_property_with_building(
	*,
	property_overrides: Optional[Dict[str, Any]] = None,
	building_overrides: Optional[Dict[str, Any]] = None,
) -> Tuple[PropertyRecord, BuildingDetail]:
	"""Create a PropertyRecord plus one BuildingDetail for test isolation."""

	property_defaults: Dict[str, Any] = {
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

	building_defaults: Dict[str, Any] = {
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


class BedroomBathroomDataTest(TestCase):
	"""Validate that ORM data for the Wall Street sample property is present."""

	@classmethod
	def setUpTestData(cls) -> None:
		cls.property, cls.building = create_property_with_building()
		cls.features = [
			ExtraFeature.objects.create(
				property=cls.property,
				account_number=cls.property.account_number,
				feature_number=1,
				feature_code="RMB",
				feature_description="Bedrooms",
				quantity=Decimal("4"),
			),
			ExtraFeature.objects.create(
				property=cls.property,
				account_number=cls.property.account_number,
				feature_number=2,
				feature_code="RMF",
				feature_description="Full Baths",
				quantity=Decimal("2"),
			),
			ExtraFeature.objects.create(
				property=cls.property,
				account_number=cls.property.account_number,
				feature_number=3,
				feature_code="RMH",
				feature_description="Half Baths",
				quantity=Decimal("1"),
			),
		]

	def test_property_exists(self) -> None:
		prop = PropertyRecord.objects.filter(account_number=TEST_ACCOUNT).first()
		self.assertIsNotNone(prop)
		assert prop is not None  # mypy/pyright hint
		self.assertEqual(prop.street_name, "WALL")
		self.assertEqual(prop.zipcode, "77040")

	def test_building_details_exist(self) -> None:
		buildings = BuildingDetail.objects.filter(account_number=TEST_ACCOUNT, is_active=True)
		self.assertEqual(buildings.count(), 1)
		building = buildings.first()
		self.assertIsNotNone(building)
		assert building is not None
		self.assertEqual(building.bedrooms, 4)

	def test_bedroom_bathroom_values(self) -> None:
		building = BuildingDetail.objects.get(account_number=TEST_ACCOUNT, building_number=1)
		self.assertEqual(building.bedrooms, 4)
		self.assertEqual(building.half_baths, 1)
		self.assertEqual(building.bathrooms, Decimal("2.5"))

	def test_extra_features_for_room_data(self) -> None:
		room_codes = ["RMB", "RMF", "RMH"]
		features = ExtraFeature.objects.filter(
			account_number=TEST_ACCOUNT,
			is_active=True,
			feature_code__in=room_codes,
		)
		self.assertEqual(features.count(), 3)
		quantities = {f.feature_code: int(f.quantity or 0) for f in features}
		self.assertEqual(quantities["RMB"], 4)
		self.assertEqual(quantities["RMF"], 2)
		self.assertEqual(quantities["RMH"], 1)

	def test_all_extra_features(self) -> None:
		features = list(
			ExtraFeature.objects.filter(account_number=TEST_ACCOUNT, is_active=True).order_by("feature_number")
		)
		self.assertEqual(len(features), 3)
		self.assertListEqual([f.feature_code for f in features], ["RMB", "RMF", "RMH"])

	def test_check_source_files(self) -> None:
		account = TEST_ACCOUNT
		with tempfile.TemporaryDirectory() as tmpdir:
			base_path = os.path.join(tmpdir, "Real_acct_ownership_history")
			os.makedirs(base_path, exist_ok=True)
			sample_path = os.path.join(base_path, "extra_features_sample.txt")

			with open(sample_path, "w", encoding="utf-8") as handle:
				handle.write("acct\tbld_num\ttype\tunits\n")
				handle.write(f"{account}\t1\tRMB\t4\n")
				handle.write(f"{account}\t1\tRMF\t2\n")
				handle.write(f"{account}\t1\tRMH\t1\n")
				handle.write("9999999999999\t1\tRMF\t3\n")

			with open(sample_path, "r", encoding="utf-8") as handle:
				matching_lines = [line.strip() for line in handle if account in line]

			room_lines = [line for line in matching_lines if any(code in line for code in ("RMB", "RMF", "RMH"))]
			self.assertEqual(len(matching_lines), 3)
			self.assertEqual(len(room_lines), 3)


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
			stats = load_fixtures_room_counts(temp_path, chunk_size=10)
		finally:
			os.unlink(temp_path)

		building = BuildingDetail.objects.get(pk=self.building.pk)
		self.assertEqual(building.bedrooms, 4)
		self.assertEqual(building.half_baths, 1)
		self.assertEqual(building.bathrooms, Decimal("2.5"))
		self.assertEqual(stats["buildings_updated"], 1)
		self.assertEqual(stats["buildings_not_found"], 1)


class ViewDisplayTest(TestCase):
	"""Verify the index view renders bedroom/bathroom counts."""

	@classmethod
	def setUpTestData(cls) -> None:
		cls.property, cls.building = create_property_with_building()

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
