"""Characterization pins for the four shared county routes, written before they change.

The comparables page, report page, CSV and PDF are driven through the Django test
client against each real county adapter. Only the comparable lookup (and, where a
test needs a given tax completeness, the tax impact) is replaced at the adapter, so
readiness, subject lookup, equity math, views, templates and renderers are all real.

Every test here passes on the code as it stands. Tests that pin behaviour a later
ticket of spec #106 changes say so in their names and docstrings, so that ticket can
update the expectation deliberately instead of deleting the pin.
"""

from __future__ import annotations

import csv
import io
import re
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, NamedTuple
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from counties.brazos.adapter import adapter as brazos_adapter
from counties.brazos.models import PropertyAccount, PropertyLand
from counties.common.contracts import Comp, CountyAdapter, ScoreComponent
from counties.common.tax_impact import TaxImpactResult
from counties.common.tests.test_shared_pages import (
    brazos_report_ready_subject,
    harris_report_ready_subject,
)
from counties.harris.adapter import adapter as harris_adapter
from counties.harris.tests.test_readiness import harris_property

COMPARABLES = "similar_properties"
REPORT = "protest_analysis"
CSV = "protest_analysis_export"
PDF = "protest_analysis_pdf"
ROUTES = (COMPARABLES, REPORT, CSV, PDF)


class County(NamedTuple):
    adapter: CountyAdapter
    key: str
    make_subject: Any
    unknown_key: str

    @property
    def slug(self) -> str:
        return self.adapter.profile.slug


HARRIS = County(harris_adapter, "SHARED001", harris_report_ready_subject, "NOSUCH001")
BRAZOS = County(brazos_adapter, "000000010013", brazos_report_ready_subject, "000000099999")
COUNTIES = (HARRIS, BRAZOS)


def get(
    client: Client,
    county: County,
    route: str,
    key: str,
    comps: list[Comp] | None = None,
    **params: str,
):
    """Request one shared route for ``key``, with the adapter's comparable lookup stubbed."""
    with patch.object(county.adapter, "find_comps", return_value=comps or []):
        return client.get(
            reverse(county.adapter.profile.url_name(route), args=[key]), params or None
        )


class UnknownKeyTests(TestCase):
    """A key that names no property, asked for on a database that does hold properties."""

    def test_comparables_page_answers_404(self):
        """Ticket #110: the comparables page 404s for an unknown key like the other routes."""
        for county in COUNTIES:
            county.make_subject()
            with self.subTest(county=county.slug):
                response = get(self.client, county, COMPARABLES, county.unknown_key)

                self.assertEqual(response.status_code, 404)
                self.assertTemplateNotUsed(response, "counties/similar_properties.html")

    def test_report_page_csv_and_pdf_answer_404(self):
        for county in COUNTIES:
            county.make_subject()
            for route in (REPORT, CSV, PDF):
                with self.subTest(county=county.slug, route=route):
                    response = get(self.client, county, route, county.unknown_key)

                    self.assertEqual(response.status_code, 404)


class HarrisRecordThatIsNotSearchReadyTests(TestCase):
    """A Harris record that is not residential or not data-ready is not found, not unavailable.

    ``HarrisAdapter.get_subject`` only returns residential, data-ready records, so for
    Harris these records take the same path as an unknown key, even though readiness
    holds a reason for them. The reason is never shown on any route. The spec's
    "found but not ready" wording does not describe this case for Harris; the genuine
    Harris found-but-unavailable cases are in ``HarrisFoundButUnavailableTests``.
    """

    def setUp(self):
        harris_report_ready_subject()
        harris_property("NOTREADY1", data_ready=False)
        harris_property("COMMERCIAL1", residential=False)
        self.reasons = {
            "NOTREADY1": "Harris data readiness incomplete",
            "COMMERCIAL1": "Not residential under Harris source classification",
        }

    def test_readiness_holds_a_reason_that_no_route_shows(self):
        for key, reason in self.reasons.items():
            with self.subTest(key=key):
                self.assertEqual(HARRIS.adapter.capabilities(key).reason_for("report"), reason)
                for route in ROUTES:
                    response = get(self.client, HARRIS, route, key)

                    self.assertNotContains(response, reason, status_code=response.status_code)

    def test_comparables_page_answers_404(self):
        """Ticket #110: the comparables page 404s for these records like the other routes."""
        for key in self.reasons:
            with self.subTest(key=key):
                response = get(self.client, HARRIS, COMPARABLES, key)

                self.assertEqual(response.status_code, 404)
                self.assertTemplateNotUsed(response, "counties/similar_properties.html")

    def test_report_page_csv_and_pdf_answer_404(self):
        for key in self.reasons:
            for route in (REPORT, CSV, PDF):
                with self.subTest(key=key, route=route):
                    response = get(self.client, HARRIS, route, key)

                    self.assertEqual(response.status_code, 404)


def add_brazos_account(prop_id: str, *, located: bool = True, equity: bool = True) -> None:
    """A Brazos account in the active snapshot of ``brazos_report_ready_subject``.

    ``located=False`` drops its coordinates (not comparable-ready); ``equity=False``
    drops its assessed value and living area but keeps land, so it stays comparable-ready
    in land mode and is refused by the report only.
    """
    PropertyAccount.objects.create(
        prop_id=prop_id,
        tax_year=2025,
        owner_name="OTHER OWNER",
        situs_address="300 OAK ST",
        situs_zip="77801",
        latitude=Decimal("30.6800000") if located else None,
        longitude=Decimal("-96.3800000") if located else None,
        coordinate_source="bcad-certified-gis" if located else "",
        coordinate_source_year=2025 if located else None,
        living_area=Decimal("2000") if equity else None,
        assessed_value=Decimal("300000") if equity else None,
        class_code="RV3",
    )
    PropertyLand.objects.create(prop_id=prop_id, tax_year=2025, land_seq=1, acreage=Decimal("0.25"))


class FoundButUnavailableMixin:
    """The four routes for a key the county finds but cannot fully serve.

    ``comparable_reason`` is None when the comparables page renders normally.
    """

    def assert_routes(
        self: Any,
        county: County,
        key: str,
        *,
        comparable_reason: str | None,
        report_reason: str,
    ) -> None:
        page = get(self.client, county, COMPARABLES, key)
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "counties/similar_properties.html")
        self.assertEqual(page.context["subject"].key, key)
        if comparable_reason is None:
            self.assertNotIn("error", page.context)
            self.assertNotContains(page, report_reason)
        else:
            self.assertEqual(page.context["error"], comparable_reason)
            self.assertContains(page, comparable_reason)

        report = get(self.client, county, REPORT, key)
        self.assertEqual(report.status_code, 200)
        self.assertTemplateUsed(report, "counties/protest_analysis.html")
        self.assertEqual(report.context["subject"].key, key)
        self.assertEqual(report.context["error"], report_reason)
        self.assertContains(report, report_reason)

        for route in (CSV, PDF):
            download = get(self.client, county, route, key)
            self.assertEqual(download.status_code, 400, route)
            self.assertEqual(download.content, report_reason.encode(), route)


class HarrisFoundButUnavailableTests(FoundButUnavailableMixin, TestCase):
    """Residential, data-ready Harris records the site finds but cannot analyse."""

    def test_record_without_coordinates_is_unavailable_on_all_four_routes(self):
        harris_report_ready_subject()
        harris_property("NOLOC002", located=False)

        self.assert_routes(
            HARRIS,
            "NOLOC002",
            comparable_reason=(
                "This property does not have location data required for similarity search."
            ),
            report_reason=(
                "This property does not have location data required for similarity search."
            ),
        )

    def test_record_without_equity_inputs_has_comparables_but_no_report(self):
        harris_report_ready_subject()
        harris_property("NOEQUITY1", equity=False)

        self.assert_routes(
            HARRIS,
            "NOEQUITY1",
            comparable_reason=None,
            report_reason="Positive assessed value, living area and coordinates are required",
        )

    def test_record_without_a_report_pool_has_comparables_but_no_report(self):
        harris_property("LONE001")

        self.assert_routes(
            HARRIS,
            "LONE001",
            comparable_reason=None,
            report_reason="At least three qualifying comparables are required",
        )


class BrazosFoundButUnavailableTests(FoundButUnavailableMixin, TestCase):
    """Brazos accounts in the active snapshot that are not comparable-ready or report-ready."""

    def setUp(self):
        brazos_report_ready_subject()

    def test_account_that_is_not_comparable_ready_is_unavailable_on_all_four_routes(self):
        add_brazos_account("000000010098", located=False)

        self.assert_routes(
            BRAZOS,
            "000000010098",
            comparable_reason="Coordinates with source provenance are unavailable.",
            report_reason="Coordinates with source provenance are unavailable.",
        )

    def test_account_without_equity_facts_has_comparables_but_no_report(self):
        add_brazos_account("000000010097", equity=False)

        self.assert_routes(
            BRAZOS,
            "000000010097",
            comparable_reason=None,
            report_reason="Positive assessed value and living area are required for a report.",
        )

    def test_account_without_a_report_pool_has_comparables_but_no_report(self):
        PropertyAccount.objects.filter(prop_id__startswith="0000000200").delete()

        self.assert_routes(
            BRAZOS,
            "000000010013",
            comparable_reason=None,
            report_reason=(
                "At least three same-mode comparable-ready properties with equity facts "
                "are required."
            ),
        )


# --------------------------------------------------------------------------- evidence fixtures


def evidence_comps() -> list[Comp]:
    """Three comparables that between them fill every CSV column and leave others blank.

    One carries every optional field, one has a formula-like address and a breakdown
    component with no points, and one is as sparse as a comparable can be.
    """
    return [
        Comp(
            key="C1",
            address="101 Oak Ln",
            similarity_score=92.5,
            match_label="Highly similar",
            assessed_value=Decimal("280000"),
            living_area=2000.0,
            bedrooms=3,
            bathrooms=2.5,
            year_built=2005,
            quality_code="B",
            condition_code="G",
            distance=0.4,
            score_breakdown=[
                ScoreComponent("living_area", "Living area", 24.0, 0.85, 20.4),
                ScoreComponent("bedrooms", "Bedrooms", 14.0, 1.0, 14.0),
            ],
        ),
        Comp(
            key="C2",
            address="=SUM(A1) Fraud Rd",
            similarity_score=81.0,
            match_label="Similar",
            assessed_value=Decimal("300000"),
            living_area=2500.0,
            bedrooms=4,
            bathrooms=3.0,
            distance=1.2,
            score_breakdown=[
                ScoreComponent("living_area", "Living area", 24.0, 0.8, 19.2),
                ScoreComponent("year_built", "Year built", 8.0, None, None),
            ],
        ),
        Comp(
            key="C3",
            address="303 Pine Ct",
            similarity_score=70.04,
            match_label="Moderately similar",
        ),
    ]


def tax_impact_result(completeness: str, warnings: list[str]) -> TaxImpactResult:
    """A tax impact with real-looking totals, as a county adapter would return it."""
    return TaxImpactResult(
        tax_year=2025,
        current_tax_owed=Decimal("12345.67"),
        median_tax_owed=Decimal("10234.50"),
        estimated_savings=Decimal("2111.17"),
        effective_rate=Decimal("0.021000"),
        current_assessed_value=Decimal("300000.00"),
        taxable_value_used=Decimal("300000.00"),
        completeness=completeness,
        warnings=warnings,
        exemptions_summary=[],
        per_unit_breakdown=[
            {
                "tax_unit_code": "A",
                "tax_unit_name": "Unit Alpha",
                "rate": Decimal("0.021000"),
                "current_taxable_value": Decimal("300000.00"),
                "median_taxable_value": Decimal("250000.00"),
                "current_tax_amount": Decimal("12345.67"),
                "median_tax_amount": Decimal("10234.50"),
                "warning": None,
            },
            {
                "tax_unit_code": "B",
                "tax_unit_name": "Unit Beta",
                "rate": None,
                "current_taxable_value": None,
                "median_taxable_value": None,
                "current_tax_amount": None,
                "median_tax_amount": None,
                "warning": "Missing tax-unit rate",
            },
        ],
    )


def pdf_lines(response) -> list[str]:
    """The text lines of a protest PDF, in page order, as the renderer wrote them."""
    stream_lines = re.findall(r"^\((.*)\) Tj$", response.content.decode("latin-1"), flags=re.M)
    return [re.sub(r"\\([\\()])", r"\1", line) for line in stream_lines]


# --------------------------------------------------------------------------- CSV literals

CSV_HEADER = (
    "address,similarity_score,similarity_label,living_area_sqft,bedrooms,bathrooms,"
    "year_built,quality_code,condition_code,assessed_value,value_per_sqft,"
    "delta_vs_subject_per_sqft,score_breakdown,tax_year_used,tax_impact_completeness,"
    "current_tax_owed,median_tax_owed,estimated_tax_savings,tax_impact_warnings,"
    "property_source_year,assessment_history_availability,comparable_shortfall"
)
#: The comparable columns, address through score_breakdown, of ``evidence_comps``.
CSV_COMP_COLUMNS = (
    "101 Oak Ln,92.5,Highly similar,2000,3,2.5,2005,B,G,280000.00,140.00,-10.00,"
    "Living area: 20.4/24.0; Bedrooms: 14.0/14.0",
    "'=SUM(A1) Fraud Rd,81.0,Similar,2500,4,3.0,,,,300000.00,120.00,-30.00,"
    "Living area: 19.2/24.0",
    "303 Pine Ct,70.0,Moderately similar,,,,,,,,,,",
)
HISTORY_UNAVAILABLE = (
    "Assessment history unavailable. Qualified property evidence remains available."
)
#: Property source year, history availability and shortfall columns, per county.
CSV_TAIL = {
    "harris": f"Not recorded,{HISTORY_UNAVAILABLE},",
    "brazos": f"2025,{HISTORY_UNAVAILABLE},",
}
SHORTFALL_TWO = (
    "Comparable shortfall: only 2 comparables meet the minimum score of 70. "
    "Lower the minimum score to find at least 3 comparables."
)
SHORTFALL_NONE = (
    "Comparable shortfall: no comparables meet the minimum score of 70. "
    "Lower the minimum score to find at least 3 comparables."
)
#: Tax columns (tax_year_used through tax_impact_warnings) per completeness. A missing
#: tax impact is what each county's real adapter returns for the report-ready fixture.
CSV_TAX = {
    "complete": "2025,complete,12345.67,10234.50,2111.17,",
    "partial": "2025,partial,,,,W one. | W two.",
    "missing": {
        "harris": (
            ",missing,,,,Published property source year is not recorded; "
            "matching-year tax impact is unavailable."
        ),
        "brazos": "2025,missing,,,,No 2025 jurisdiction and exemption rows for this property",
    },
}


def csv_text(*rows: str) -> str:
    return "".join(f"{row}\r\n" for row in (CSV_HEADER, *rows))


def for_completeness(table: dict[str, Any], county: County, completeness: str) -> Any:
    """``table[completeness]``, narrowed to the county when that entry differs by county."""
    value = table[completeness]
    return value[county.slug] if isinstance(value, dict) else value


@contextmanager
def tax_impact_as(county: County, completeness: str):
    """Make the county's tax impact complete or partial; missing is the real adapter's own."""
    if completeness == "missing":
        yield
        return
    warnings = ["W one.", "W two."] if completeness == "partial" else []
    result = tax_impact_result(completeness, warnings)
    with patch.object(county.adapter, "tax_impact", return_value=result):
        yield


# --------------------------------------------------------------------------- PDF literals

PDF_HEAD = {
    "harris": [
        "Harris County Property Tax Protest Evidence Report",
        "Account: SHARED001",
        "Property: 1 Shared St",
        "Assessed Value: $300,000",
        "Living Area: 2,000 sqft",
        "Subject Value/Sqft: $150.00",
        "Property Source Year: Not recorded",
        HISTORY_UNAVAILABLE,
    ],
    "brazos": [
        "Brazos County Property Tax Protest Evidence Report",
        "Property ID: 000000010013",
        "Property: 100 MAIN ST",
        "Assessed Value: $300,000",
        "Living Area: 2,000 sqft",
        "Subject Value/Sqft: $150.00",
        "Property Source Year: 2025",
        HISTORY_UNAVAILABLE,
        "",
        "Assessment History",
        "2025: -, YoY -, Needs review",
        "2024: -, YoY -, Needs review",
        "2023: -, YoY -, Needs review",
        "2022: -, YoY -, Needs review",
        "2021: -, YoY -, Needs review",
    ],
}
PDF_COMPS = [
    "",
    "Comparable Evidence",
    "101 Oak Ln: score 92.5, $140.00/sqft",
    "=SUM(A1) Fraud Rd: score 81.0, $120.00/sqft",
    "303 Pine Ct: score 70.0",
]
#: Ticket #112: the PDF states the page's sentence (it read "Tax totals unavailable until
#: matching-year inputs are complete." before). The PDF does not wrap text, so the renderer
#: breaks this one sentence over two lines to keep it inside the page.
PDF_TAX_UNAVAILABLE = [
    "Tax totals unavailable until all applicable matching-year jurisdiction,",
    "exemption, and rate inputs are complete.",
]
PDF_TAX = {
    "complete": [
        "",
        "Tax Impact (Estimated)",
        "Tax Year Used: 2025 (complete)",
        "Current Taxes Owed: $12,345.67",
        "Median-Scenario Taxes Owed: $10,234.50",
        "Estimated Annual Savings: $2,111.17",
    ],
    "partial": [
        "",
        "Tax Impact (Estimated)",
        "Tax Year Used: 2025 (partial)",
        *PDF_TAX_UNAVAILABLE,
        "Warnings: W one. | W two.",
    ],
    "missing": {
        "harris": [
            "",
            "Tax Impact (Estimated)",
            "Tax Year Used: - (missing)",
            *PDF_TAX_UNAVAILABLE,
            "Warnings: Published property source year is not recorded; "
            "matching-year tax impact is unavailable.",
        ],
        "brazos": [
            "",
            "Tax Impact (Estimated)",
            "Tax Year Used: 2025 (missing)",
            *PDF_TAX_UNAVAILABLE,
            "Warnings: No 2025 jurisdiction and exemption rows for this property",
        ],
    },
}

# --------------------------------------------------------------------------- page literals

PAGE_TAX_UNAVAILABLE = (
    "Tax totals unavailable until all applicable matching-year jurisdiction, "
    "exemption, and rate inputs are complete."
)
#: Badge classes and label beside the page's "Tax Impact" heading: the only visible
#: difference between a partial and a missing tax impact.
PAGE_TAX_BADGE = {
    "complete": ("bg-green-100 text-green-800", "Complete"),
    "partial": ("bg-amber-100 text-amber-800", "Partial"),
    "missing": ("bg-red-100 text-red-800", "Missing"),
}
PAGE_TAX_WARNINGS = {
    "complete": [],
    "partial": ["W one.", "W two."],
    "missing": {
        "harris": [
            "Published property source year is not recorded; "
            "matching-year tax impact is unavailable."
        ],
        "brazos": ["No 2025 jurisdiction and exemption rows for this property"],
    },
}
COMPLETENESS = ("complete", "partial", "missing")


class TaxCompletenessTests(TestCase):
    """Tax completeness is a bare string, compared separately by the page, CSV and PDF.

    ``complete`` shows the totals. ``partial`` and ``missing`` withhold them on every
    surface, and the page alone tells the two apart (badge colour and label); the CSV and
    PDF print the completeness word. ``partial`` and ``complete`` are stubbed at the
    adapter's ``tax_impact``; ``missing`` is each county's real unavailable result.
    """

    def setUp(self):
        for county in COUNTIES:
            county.make_subject()

    def _respond(self, county: County, route: str, completeness: str):
        with tax_impact_as(county, completeness):
            return get(self.client, county, route, county.key, evidence_comps())

    def test_page_shows_totals_only_when_complete(self):
        for county in COUNTIES:
            for completeness in COMPLETENESS:
                with self.subTest(county=county.slug, tax=completeness):
                    response = self._respond(county, REPORT, completeness)

                    self.assertEqual(response.status_code, 200)
                    html = response.content.decode()
                    badge = re.search(
                        r'Tax Impact</h2>\s*<span class="text-xs px-2 py-1 rounded-full '
                        r'([^"]+)">\s*(\w+)\s*</span>',
                        html,
                    )
                    self.assertIsNotNone(badge)
                    self.assertEqual(badge.groups(), PAGE_TAX_BADGE[completeness])
                    self.assertEqual(response.context["tax_impact"].completeness, completeness)
                    totals = (
                        "Current Taxes Owed",
                        "$12,345.67",
                        "Taxes at Median Assessed Value",
                        "$10,234.50",
                        "Estimated Annual Savings",
                        "$2,111.17",
                        "Year 2025",
                        "Total rate 0.021000",
                    )
                    for text in totals:
                        if completeness == "complete":
                            self.assertContains(response, text)
                        elif text.startswith("$"):
                            continue  # a partial result's per-unit rows still print dollars
                        else:
                            self.assertNotContains(response, text)
                    if completeness == "complete":
                        self.assertNotContains(response, PAGE_TAX_UNAVAILABLE)
                    else:
                        self.assertContains(response, PAGE_TAX_UNAVAILABLE)
                    for warning in for_completeness(PAGE_TAX_WARNINGS, county, completeness):
                        self.assertContains(response, f"⚠ {warning}")

    def test_page_still_prints_per_unit_rows_for_a_partial_tax_impact(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                response = self._respond(county, REPORT, "partial")

                self.assertContains(response, "Unit Alpha (A)")
                self.assertContains(response, "$12,345.67")
                self.assertContains(response, "Missing tax-unit rate")

    def test_csv_prints_totals_only_when_complete(self):
        for county in COUNTIES:
            for completeness in COMPLETENESS:
                with self.subTest(county=county.slug, tax=completeness):
                    response = self._respond(county, CSV, completeness)

                    tail = CSV_TAIL[county.slug]
                    tax = for_completeness(CSV_TAX, county, completeness)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        response.content.decode(),
                        csv_text(*(f"{comp},{tax},{tail}" for comp in CSV_COMP_COLUMNS)),
                    )

    def test_pdf_prints_totals_only_when_complete(self):
        for county in COUNTIES:
            for completeness in COMPLETENESS:
                with self.subTest(county=county.slug, tax=completeness):
                    response = self._respond(county, PDF, completeness)

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        pdf_lines(response),
                        [
                            *PDF_HEAD[county.slug],
                            *PDF_COMPS,
                            *for_completeness(PDF_TAX, county, completeness),
                        ],
                    )


class ProtestCsvOutputTests(TestCase):
    """The CSV's exact bytes for today's cases, so a later ticket can prove it did not move them.

    The tax-complete, partial and missing files are pinned in ``TaxCompletenessTests``.
    Each county's real unavailable tax impact is used here.
    """

    def setUp(self):
        for county in COUNTIES:
            county.make_subject()

    def test_csv_is_an_attachment_named_for_the_property(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                response = get(self.client, county, CSV, county.key, evidence_comps())

                self.assertEqual(response["Content-Type"], "text/csv")
                self.assertEqual(
                    response["Content-Disposition"],
                    f'attachment; filename="protest_analysis_{county.key}.csv"',
                )

    def test_two_comparables_state_the_shortfall_on_every_row(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                response = get(self.client, county, CSV, county.key, evidence_comps()[:2])

                tax = for_completeness(CSV_TAX, county, "missing")
                tail = CSV_TAIL[county.slug]
                self.assertEqual(
                    response.content.decode(),
                    csv_text(
                        *(f"{comp},{tax},{tail}{SHORTFALL_TWO}" for comp in CSV_COMP_COLUMNS[:2])
                    ),
                )

    def test_no_comparables_leave_one_notice_only_row(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                response = get(self.client, county, CSV, county.key, [])

                self.assertEqual(response.content.decode(), csv_text("," * 21 + SHORTFALL_NONE))


class ComparableCountTests(TestCase):
    """The page and CSV list every comparable found; the PDF lists only ten of them.

    The report routes ask the adapter for up to 50 comparables. These tests stub
    the adapter to return more than ten and compare what each surface prints, side by
    side. Comparables come back from the adapter worst first, so the PDF's ten are the
    ten best by the shared display order, not the first ten returned.
    """

    @staticmethod
    def numbered_comps(total: int) -> list[Comp]:
        best_first = [
            Comp(
                key=f"N{number:02d}",
                address=f"Comp {number:02d} Ln",
                similarity_score=100.0 - number / 2,
                match_label="Highly similar",
                assessed_value=Decimal("250000"),
                living_area=2000.0,
            )
            for number in range(total)
        ]
        return best_first[::-1]

    def setUp(self):
        for county in COUNTIES:
            county.make_subject()

    def test_pdf_lists_ten_comparables_while_page_and_csv_list_all_ticket_114_adds_a_note(self):
        """Today nothing on the PDF says comparables were left out; #114 adds that note."""
        for county in COUNTIES:
            for total in (11, 12, 50):
                with self.subTest(county=county.slug, comparables=total):
                    comps = self.numbered_comps(total)
                    addresses = [f"Comp {number:02d} Ln" for number in range(total)]

                    page = get(self.client, county, REPORT, county.key, comps)
                    csv_response = get(self.client, county, CSV, county.key, comps)
                    pdf = get(self.client, county, PDF, county.key, comps)

                    self.assertEqual(
                        [row.comp.address for row in page.context["comp_rows"]], addresses
                    )
                    rendered = re.findall(r"Comp \d\d Ln", page.content.decode())
                    self.assertEqual(sorted(set(rendered)), sorted(addresses))
                    csv_rows = list(csv.DictReader(io.StringIO(csv_response.content.decode())))
                    self.assertEqual([row["address"] for row in csv_rows], addresses)
                    body = pdf.content.decode("latin-1")
                    self.assertEqual(re.findall(r"\((Comp \d\d Ln): score", body), addresses[:10])
                    self.assertNotIn("Showing", body)
                    self.assertNotIn("closest", body)

    def test_report_routes_ask_the_adapter_for_up_to_50_comparables(self):
        for county in COUNTIES:
            for route in (REPORT, CSV, PDF):
                with self.subTest(county=county.slug, route=route):
                    with patch.object(county.adapter, "find_comps", return_value=[]) as find_comps:
                        self.client.get(
                            reverse(county.adapter.profile.url_name(route), args=[county.key])
                        )

                    find_comps.assert_called_once_with(
                        county.key, max_distance_miles=10.0, max_results=50, min_score=70.0
                    )
