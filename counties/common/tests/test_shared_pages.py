"""The invariant this package exists to hold: every county gets the same pages.

Counties differ in their ETL and their models; the search / compare / analyse
surface must not. These tests assert that structurally rather than per-county,
so adding a third county cannot quietly ship a narrower site.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from inspect import unwrap
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.urls import NoReverseMatch, resolve, reverse

from counties.brazos.adapter import adapter as brazos_adapter
from counties.brazos.models import (
    BrazosPropertySnapshot,
    PropertyAccount,
    PropertyLand,
    SnapshotOutcome,
)
from counties.common.analysis import recommend_protest, summarize_equity
from counties.common.contracts import Comp, Subject
from counties.common.urls import _ROUTES
from counties.harris.adapter import adapter as harris_adapter
from counties.harris.models import BuildingDetail, PropertyRecord
from counties.harris.tests.test_readiness import add_distant_report_pool

ADAPTERS = (harris_adapter, brazos_adapter)


def brazos_report_ready_subject():
    """Brazos subject 000000010013, report-ready with three nearby comparables."""
    BrazosPropertySnapshot.objects.create(
        tax_year=2025,
        outcome=SnapshotOutcome.COMPLETED,
        cad_source_year=2025,
        gis_source_year=2025,
    )
    PropertyAccount.objects.create(
        prop_id="000000010013",
        tax_year=2025,
        owner_name="TARGET OWNER",
        situs_address="100 MAIN ST",
        situs_zip="77801",
        latitude=Decimal("30.6700000"),
        longitude=Decimal("-96.3700000"),
        coordinate_source="bcad-certified-gis",
        coordinate_source_year=2025,
        living_area=Decimal("2000"),
        assessed_value=Decimal("300000"),
        class_code="RV3",
    )
    PropertyLand.objects.create(
        prop_id="000000010013", tax_year=2025, land_seq=1, acreage=Decimal("0.2500")
    )
    for index in range(1, 4):
        prop_id = f"0000000200{index:02d}"
        PropertyAccount.objects.create(
            prop_id=prop_id,
            tax_year=2025,
            owner_name=f"COMP {index}",
            situs_address=f"{100 + index} MAIN ST",
            situs_zip="77801",
            latitude=Decimal("30.6701000") + Decimal(index) / Decimal("100000"),
            longitude=Decimal("-96.3701000"),
            coordinate_source="bcad-certified-gis",
            coordinate_source_year=2025,
            living_area=Decimal("2000"),
            assessed_value=Decimal("250000"),
            class_code="RV3",
        )
        PropertyLand.objects.create(
            prop_id=prop_id, tax_year=2025, land_seq=1, acreage=Decimal("0.2500")
        )


def harris_report_ready_subject():
    """Harris subject SHARED001, report-ready against a distant qualifying pool."""
    add_distant_report_pool()
    prop = PropertyRecord.objects.create(
        address="1 Shared St",
        city="Houston",
        zipcode="77001",
        owner_name="Shared Owner",
        account_number="SHARED001",
        is_residential=True,
        is_data_ready=True,
        street_number="1",
        street_name="Shared St",
        assessed_value=300000,
        building_area=2000,
        latitude=29.8,
        longitude=-95.5,
    )
    BuildingDetail.objects.create(
        property=prop,
        account_number=prop.account_number,
        building_number=1,
        heat_area=2000,
        is_active=True,
    )


class EveryCountyHasTheSamePagesTests(SimpleTestCase):
    def test_all_shared_routes_reverse_for_every_county(self):
        for adapter in ADAPTERS:
            for _view, suffix, name in _ROUTES:
                url_name = adapter.profile.url_name(name)
                args = ["KEY123"] if "<str:key>" in suffix else []
                try:
                    reverse(url_name, args=args)
                except NoReverseMatch:  # pragma: no cover - failure path
                    self.fail(f"{adapter.profile.slug} is missing the {name!r} page ({url_name})")

    def test_every_route_is_served_by_the_shared_view(self):
        for adapter in ADAPTERS:
            for view, suffix, name in _ROUTES:
                args = ["KEY123"] if "<str:key>" in suffix else []
                match = resolve(reverse(adapter.profile.url_name(name), args=args))
                # county_urlpatterns binds the adapter with functools.partial.
                self.assertIs(unwrap(match.func.func), view)
                self.assertIs(match.func.keywords["adapter"], adapter)

    def test_counties_do_not_collide_on_url_names(self):
        names = [
            adapter.profile.url_name(name)
            for adapter in ADAPTERS
            for _view, _suffix, name in _ROUTES
        ]
        self.assertEqual(len(names), len(set(names)))

    def test_each_county_declares_the_columns_its_pages_need(self):
        for adapter in ADAPTERS:
            profile = adapter.profile
            self.assertTrue(profile.search_fields, f"{profile.slug} has no search fields")
            self.assertTrue(profile.search_columns, f"{profile.slug} has no result columns")
            self.assertTrue(profile.comp_columns, f"{profile.slug} has no comparable columns")


class SharedEquityMathTests(SimpleTestCase):
    """The analysis is county-neutral, so it is tested on neutral records."""

    def _comp(self, key, assessed, area, score=80.0):
        return Comp(
            key=key,
            address=f"{key} Test St",
            similarity_score=score,
            match_label="Highly similar",
            assessed_value=Decimal(assessed),
            living_area=area,
        )

    def test_equity_gap_and_savings(self):
        subject = Subject(
            key="S1",
            address_line="1 Test St",
            assessed_value=Decimal("370000"),
            living_area=2000.0,
        )
        comps = [self._comp("C1", "320000", 2000.0), self._comp("C2", "340000", 2000.0)]

        equity = summarize_equity(subject, comps)

        self.assertAlmostEqual(equity.subject_value_per_sqft, 185.0)
        self.assertAlmostEqual(equity.median_comp_value_per_sqft, 165.0)
        self.assertAlmostEqual(equity.equity_gap_per_sqft, 20.0)
        self.assertAlmostEqual(equity.estimated_savings, 40000.0)
        self.assertEqual(equity.comps_below_subject, 2)
        self.assertEqual(equity.median_assessed_value, Decimal("330000.00"))

    def test_savings_never_goes_negative(self):
        subject = Subject(
            key="S1",
            address_line="1 Test St",
            assessed_value=Decimal("200000"),
            living_area=2000.0,
        )
        equity = summarize_equity(subject, [self._comp("C1", "400000", 2000.0)])

        self.assertLess(equity.equity_gap_per_sqft, 0)
        self.assertEqual(equity.estimated_savings, 0.0)

    def test_equity_is_empty_without_a_subject_price_per_sqft(self):
        subject = Subject(key="S1", address_line="1 Test St", assessed_value=None, living_area=None)
        equity = summarize_equity(subject, [self._comp("C1", "400000", 2000.0)])

        self.assertIsNone(equity.subject_value_per_sqft)
        self.assertIsNone(equity.median_assessed_value)
        self.assertEqual(equity.qualifying_comp_count, 1)

    def test_recommendation_needs_three_comparables(self):
        comps = [self._comp("C1", "100000", 1000.0), self._comp("C2", "110000", 1000.0)]
        self.assertIsNone(recommend_protest(200.0, comps))

        comps.append(self._comp("C3", "120000", 1000.0))
        self.assertIsNotNone(recommend_protest(200.0, comps))


class BrazosGainsTheSharedPagesTests(TestCase):
    """Brazos previously had only search + report; the shared layer adds the rest."""

    def setUp(self):
        brazos_report_ready_subject()

    def test_comparables_page_renders(self):
        response = self.client.get(reverse("brazos_similar_properties", args=["000000010013"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "100 MAIN ST")
        # Brazos-specific vocabulary on a shared template.
        self.assertContains(response, "Property ID")
        self.assertContains(response, "Class Code")
        self.assertContains(response, "Similar Properties Found")

    def test_search_export_returns_csv(self):
        response = self.client.get(reverse("brazos_export_csv"), {"owner_name": "TARGET"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("Prop ID", response.content.decode().splitlines()[0])

    def test_search_export_requires_a_meaningful_filter(self):
        response = self.client.get(reverse("brazos_export_csv"))
        self.assertEqual(response.status_code, 400)

    def test_protest_csv_export_returns_csv(self):
        response = self.client.get(reverse("brazos_protest_analysis_export", args=["000000010013"]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("similarity_score", response.content.decode().splitlines()[0])

    def test_protest_pdf_export_returns_pdf(self):
        response = self.client.get(reverse("brazos_protest_analysis_pdf", args=["000000010013"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        body = response.content.decode("latin-1", errors="ignore")
        self.assertIn("Brazos County Property Tax Protest Evidence Report", body)
        self.assertIn("Property ID: 000000010013", body)


class NoLocationSubjectTests(TestCase):
    """A subject with no lat/long can't run comparable search -- every protest
    view must say so consistently rather than some silently degrading."""

    def setUp(self):
        PropertyRecord.objects.create(
            address="1 No Location St",
            city="Houston",
            zipcode="77001",
            owner_name="No Location Owner",
            account_number="NOLOC001",
            is_residential=True,
            is_data_ready=True,
            street_number="1",
            street_name="No Location St",
            assessed_value=300000,
            building_area=2000,
            latitude=None,
            longitude=None,
        )

    def test_html_report_shows_the_no_location_banner(self):
        response = self.client.get(reverse("protest_analysis", args=["NOLOC001"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "does not have location data")

    def test_csv_export_is_a_bad_request_not_a_silent_empty_file(self):
        response = self.client.get(reverse("protest_analysis_export", args=["NOLOC001"]))
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"does not have location data", response.content)

    def test_pdf_export_is_a_bad_request_not_a_silent_empty_report(self):
        response = self.client.get(reverse("protest_analysis_pdf", args=["NOLOC001"]))
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"does not have location data", response.content)


class HarrisPagesStillUseTheirOwnLabelsTests(TestCase):
    """Sharing the templates must not flatten each county's own vocabulary."""

    def setUp(self):
        harris_report_ready_subject()

    def test_harris_report_says_harris(self):
        response = self.client.get(reverse("protest_analysis", args=["SHARED001"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Harris County Appraisal District")
        self.assertContains(response, "HCAD")
        self.assertNotContains(response, "BCAD")
        # Harris calls its identifier an Account; Brazos calls it a Property ID.
        self.assertContains(response, "Account")

    def test_brazos_search_shows_its_coverage_caveat_and_harris_does_not(self):
        brazos = self.client.get(reverse("brazos_index"))
        self.assertContains(brazos, "Partial data")

        harris = self.client.get(reverse("index"))
        self.assertNotContains(harris, "Partial data")


class ComparableShortfallTests(TestCase):
    """A report-ready dossier with fewer than three comparables says so, in both counties.

    Readiness stays real; only the comparables search is replaced so the number of
    comparables found at the chosen minimum score is exact.
    """

    SUBJECTS = (
        (harris_adapter, "SHARED001", harris_report_ready_subject),
        (brazos_adapter, "000000010013", brazos_report_ready_subject),
    )
    NOTICE = "Comparable shortfall"

    def _comps(self, count):
        return [
            Comp(
                key=f"COMP{index}",
                address=f"{index} Comparable Ln",
                similarity_score=88.0,
                match_label="Highly similar",
                assessed_value=Decimal("250000"),
                living_area=2000.0,
            )
            for index in range(count)
        ]

    def _get(self, adapter, route, key, count, **params):
        with patch.object(adapter, "find_comps", return_value=self._comps(count)):
            return self.client.get(
                reverse(adapter.profile.url_name(route), args=[key]), params or None
            )

    def test_report_page_states_the_shortfall_below_three_comparables(self):
        expected = {
            0: "no comparables meet the minimum score of 70",
            1: "only 1 comparable meets the minimum score of 70",
            2: "only 2 comparables meet the minimum score of 70",
        }
        for adapter, key, make_subject in self.SUBJECTS:
            make_subject()
            for count, headline in expected.items():
                with self.subTest(county=adapter.profile.slug, comps=count):
                    response = self._get(adapter, "protest_analysis", key, count)
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, self.NOTICE)
                    self.assertContains(response, headline)
                    self.assertContains(response, "Lower the minimum score")

    def test_report_page_has_no_shortfall_at_three_or_more_comparables(self):
        for adapter, key, make_subject in self.SUBJECTS:
            make_subject()
            for count in (3, 4):
                with self.subTest(county=adapter.profile.slug, comps=count):
                    response = self._get(adapter, "protest_analysis", key, count)
                    self.assertEqual(response.status_code, 200)
                    self.assertNotContains(response, self.NOTICE)

    def test_equity_banner_still_renders_alongside_the_shortfall(self):
        for adapter, key, make_subject in self.SUBJECTS:
            make_subject()
            for count in (1, 2):
                with self.subTest(county=adapter.profile.slug, comps=count):
                    response = self._get(adapter, "protest_analysis", key, count)
                    self.assertContains(response, "Potential Equity Protest Case")
                    self.assertContains(response, f"the median of {count} comparable")

    def test_shortfall_at_the_lowest_minimum_score_does_not_suggest_lowering_it(self):
        for adapter, key, make_subject in self.SUBJECTS:
            make_subject()
            with self.subTest(county=adapter.profile.slug):
                response = self._get(adapter, "protest_analysis", key, 1, min_score="52")
                self.assertContains(response, "only 1 comparable meets the minimum score of 52")
                self.assertContains(response, "already at its lowest setting")
                self.assertNotContains(response, "Lower the minimum score")

    def test_csv_export_states_the_shortfall_like_the_page(self):
        for adapter, key, make_subject in self.SUBJECTS:
            make_subject()
            for count in (0, 1, 2, 3):
                with self.subTest(county=adapter.profile.slug, comps=count):
                    response = self._get(adapter, "protest_analysis_export", key, count)
                    self.assertEqual(response.status_code, 200)
                    rows = list(csv.DictReader(io.StringIO(response.content.decode())))
                    shortfalls = {row["comparable_shortfall"] for row in rows}
                    if count >= 3:
                        self.assertEqual(len(rows), count)
                        self.assertEqual(shortfalls, {""})
                        continue
                    # A zero-comparable file still carries one row stating the shortfall.
                    self.assertEqual(len(rows), max(count, 1))
                    (notice,) = shortfalls
                    self.assertIn(self.NOTICE, notice)
                    self.assertIn("minimum score of 70", notice)
                    self.assertIn("Lower the minimum score", notice)
                    if count == 0:
                        self.assertEqual(rows[0]["address"], "")
                        self.assertEqual(rows[0]["similarity_score"], "")

    def test_pdf_export_states_the_shortfall_like_the_page(self):
        for adapter, key, make_subject in self.SUBJECTS:
            make_subject()
            for count in (0, 1, 2, 3):
                with self.subTest(county=adapter.profile.slug, comps=count):
                    response = self._get(adapter, "protest_analysis_pdf", key, count)
                    self.assertEqual(response.status_code, 200)
                    body = response.content.decode("latin-1")
                    if count >= 3:
                        self.assertNotIn(self.NOTICE, body)
                        continue
                    self.assertIn(self.NOTICE, body)
                    self.assertIn("minimum score of 70", body)
                    self.assertIn("Lower the minimum score", body)
