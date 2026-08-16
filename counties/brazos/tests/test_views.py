"""Tests for the Brazos County pages, rendered by the shared county web layer."""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from counties.brazos.models import PropertyAccount, PropertyLand
from counties.common.tax_models import ParcelGeometry


class BrazosIndexViewTests(TestCase):
    def setUp(self):
        PropertyAccount.objects.create(
            prop_id="000000010002",
            tax_year=2025,
            owner_name="STASNY FAMILY RANCH LLC",
            situs_address="7932 DRUMMER CIR",
            situs_city="COLLEGE STATION",
            situs_state="TX",
            situs_zip="77845-8087",
        )
        PropertyAccount.objects.create(
            prop_id="000000010003",
            tax_year=2025,
            owner_name="SMITH JOHN",
            situs_address="512 HELENA ST",
            situs_city="BRYAN",
            situs_state="TX",
            situs_zip="77801",
        )
        PropertyLand.objects.create(
            prop_id="000000010002",
            tax_year=2025,
            land_seq=1,
            land_value=Decimal("28336.77"),
            acreage=Decimal("10.5024"),
        )

    def test_no_filters_renders_empty_results(self):
        response = self.client.get(reverse("brazos_index"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["results"], [])
        self.assertFalse(response.context["filters_applied"])

    def test_owner_name_filter_returns_matching_account_with_land_totals(self):
        response = self.client.get(reverse("brazos_index"), {"owner_name": "STASNY"})
        self.assertEqual(response.status_code, 200)
        results = response.context["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["prop_id"], "000000010002")
        self.assertEqual(results[0]["land_value"], Decimal("28336.77"))
        self.assertEqual(results[0]["acreage"], Decimal("10.5024"))
        self.assertContains(response, "STASNY FAMILY RANCH LLC")

    def test_owner_name_filter_excludes_non_matching_account(self):
        response = self.client.get(reverse("brazos_index"), {"owner_name": "STASNY"})
        self.assertNotContains(response, "SMITH JOHN")

    def test_address_filter(self):
        response = self.client.get(reverse("brazos_index"), {"address": "HELENA"})
        results = response.context["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["prop_id"], "000000010003")

    def test_zip_filter(self):
        response = self.client.get(reverse("brazos_index"), {"zip_code": "77801"})
        results = response.context["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["prop_id"], "000000010003")

    def test_account_with_no_land_rows_shows_none(self):
        response = self.client.get(reverse("brazos_index"), {"owner_name": "SMITH"})
        results = response.context["results"]
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["land_value"])

    def test_account_without_coordinates_has_no_location(self):
        response = self.client.get(reverse("brazos_index"), {"owner_name": "SMITH"})
        results = response.context["results"]
        self.assertIsNone(results[0]["protest_url"])
        self.assertIsNone(results[0]["similar_url"])
        self.assertNotContains(response, "/brazos/protest/000000010003/")


class BrazosIndexNoDataLoadedTests(TestCase):
    """No PropertyAccount rows at all -- active_year() is None, so
    search_queryset() takes its "nothing loaded yet" branch."""

    def test_search_with_a_filter_renders_empty_results_not_an_error(self):
        response = self.client.get(reverse("brazos_index"), {"owner_name": "ANYONE"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["results"], [])

    def test_export_with_a_filter_renders_an_empty_csv_not_an_error(self):
        response = self.client.get(reverse("brazos_export_csv"), {"owner_name": "ANYONE"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])


class ProtestAnalysisViewTests(TestCase):
    def setUp(self):
        self.target = PropertyAccount.objects.create(
            prop_id="000000010013",
            tax_year=2025,
            owner_name="TARGET OWNER",
            situs_address="100 MAIN ST",
            living_area=Decimal("2000"),
            assessed_value=Decimal("300000"),
            class_code="RV3",
            year_built=2005,
        )
        ParcelGeometry.objects.create(
            account_number="000000010013",
            county="brazos",
            latitude=30.585,
            longitude=-96.298,
        )

    def test_unknown_prop_id_404s(self):
        response = self.client.get(reverse("brazos_protest_analysis", args=["NOPE"]))
        self.assertEqual(response.status_code, 404)

    def test_renders_with_no_comps(self):
        response = self.client.get(reverse("brazos_protest_analysis", args=["000000010013"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["comps"], [])
        self.assertContains(response, "100 MAIN ST")

    def test_missing_coordinates_renders_error_state(self):
        PropertyAccount.objects.create(prop_id="000000099999", tax_year=2025)
        response = self.client.get(reverse("brazos_protest_analysis", args=["000000099999"]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("location data", response.context["error"])

    def test_min_score_clamped_to_valid_range(self):
        response = self.client.get(
            reverse("brazos_protest_analysis", args=["000000010013"]), {"min_score": "10"}
        )
        self.assertEqual(response.context["min_score"], 52.0)
