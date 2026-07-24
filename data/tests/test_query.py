from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from data.models import BuildingDetail, PropertyRecord
from data.query import build_property_search_queryset


class StreetAddressSearchTests(TestCase):
    def setUp(self):
        PropertyRecord.objects.create(
            account_number="A001",
            address="5712 Main St",
            street_number="5712",
            street_name="Main St",
            zipcode="77001",
            owner_name="Alice Anderson",
        )
        PropertyRecord.objects.create(
            account_number="A002",
            address="100 Oak Dr",
            street_number="100",
            street_name="Oak Dr",
            zipcode="77002",
            owner_name="Bob Brown",
        )
        # A street name that is itself numeric, e.g. Houston's "FM 1960".
        PropertyRecord.objects.create(
            account_number="A003",
            address="8200 FM 1960",
            street_number="8200",
            street_name="FM 1960",
            zipcode="77069",
            owner_name="Carol Cooper",
        )

    def _search(self, street_address):
        qs = build_property_search_queryset({"street_address": street_address})
        return set(qs.values_list("account_number", flat=True))

    def test_number_only_matches(self):
        self.assertEqual(self._search("5712"), {"A001"})

    def test_name_only_matches(self):
        self.assertEqual(self._search("Oak"), {"A002"})

    def test_number_and_name_together_match(self):
        self.assertEqual(self._search("5712 Main"), {"A001"})

    def test_mismatched_number_and_name_finds_nothing(self):
        self.assertEqual(self._search("100 Main"), set())

    def test_numeric_street_name_is_found_by_its_number(self):
        self.assertEqual(self._search("1960"), {"A003"})

    def test_numeric_street_name_is_found_with_house_number_too(self):
        self.assertEqual(self._search("8200 1960"), {"A003"})


class AdvancedSearchFilterTests(TestCase):
    def setUp(self):
        small_old = PropertyRecord.objects.create(
            account_number="B001",
            address="1 Small St",
            street_number="1",
            street_name="Small St",
            zipcode="77003",
            assessed_value=Decimal("150000"),
        )
        BuildingDetail.objects.create(
            property=small_old,
            account_number="B001",
            building_number=1,
            heat_area=Decimal("1200"),
            bedrooms=2,
            bathrooms=Decimal("1.0"),
            year_built=1975,
            is_active=True,
        )

        big_new = PropertyRecord.objects.create(
            account_number="B002",
            address="2 Big St",
            street_number="2",
            street_name="Big St",
            zipcode="77003",
            assessed_value=Decimal("450000"),
        )
        BuildingDetail.objects.create(
            property=big_new,
            account_number="B002",
            building_number=1,
            heat_area=Decimal("3200"),
            bedrooms=5,
            bathrooms=Decimal("3.5"),
            year_built=2015,
            is_active=True,
        )

        # An inactive (superseded) building row that must not leak into results.
        BuildingDetail.objects.create(
            property=small_old,
            account_number="B001",
            building_number=2,
            heat_area=Decimal("5000"),
            bedrooms=6,
            bathrooms=Decimal("5.0"),
            year_built=2020,
            is_active=False,
        )

    def _search(self, **params):
        qs = build_property_search_queryset(params)
        return set(qs.values_list("account_number", flat=True))

    def test_min_value_filters_by_assessed_value(self):
        self.assertEqual(self._search(min_value="300000"), {"B002"})

    def test_max_value_filters_by_assessed_value(self):
        self.assertEqual(self._search(max_value="300000"), {"B001"})

    def test_min_sqft_uses_active_building_only(self):
        self.assertEqual(self._search(min_sqft="3000"), {"B002"})

    def test_min_bedrooms_and_bathrooms_combine(self):
        self.assertEqual(self._search(min_bedrooms="4", min_bathrooms="3"), {"B002"})

    def test_year_built_range(self):
        self.assertEqual(self._search(min_year_built="2000", max_year_built="2018"), {"B002"})

    def test_inactive_building_is_not_matched(self):
        self.assertEqual(self._search(min_sqft="4000"), set())

    def test_no_advanced_filters_returns_everything(self):
        self.assertEqual(self._search(), {"B001", "B002"})
