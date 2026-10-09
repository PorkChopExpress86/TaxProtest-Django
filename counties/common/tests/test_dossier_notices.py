"""Ticket #112: each notice and policy sentence is stated once, and every surface prints it.

Driven through the four shared routes against both real county adapters, as the
characterization pins are, with the builders checked against the fake adapter in
``test_analysis.py``. The sentences are written out here as literals, not read back from
the code under test.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from counties.common.contracts import PropertyCapabilities
from counties.common.tests.test_shared_route_characterization import (
    COMPARABLES,
    COUNTIES,
    CSV,
    PAGE_TAX_UNAVAILABLE,
    PDF,
    REPORT,
    County,
    evidence_comps,
    get,
    pdf_lines,
    tax_impact_as,
)


def pdf_text(response) -> str:
    """The PDF's text lines joined into one string, so a sentence the PDF wraps still reads whole."""
    return " ".join(pdf_lines(response))


class TaxTotalsWithheldNoticeTests(TestCase):
    """One sentence says tax totals are withheld, on the page and on the PDF.

    The CSV states it by leaving the totals blank. Before ticket #112 the PDF read
    "Tax totals unavailable until matching-year inputs are complete."
    """

    def setUp(self):
        for county in COUNTIES:
            county.make_subject()

    def _respond(self, county: County, route: str, completeness: str):
        with tax_impact_as(county, completeness):
            return get(self.client, county, route, county.key, evidence_comps())

    def test_page_and_pdf_state_the_same_sentence_when_totals_are_withheld(self):
        for county in COUNTIES:
            for completeness in ("partial", "missing"):
                with self.subTest(county=county.slug, tax=completeness):
                    page = self._respond(county, REPORT, completeness)
                    pdf = self._respond(county, PDF, completeness)

                    self.assertContains(page, PAGE_TAX_UNAVAILABLE)
                    self.assertIn(PAGE_TAX_UNAVAILABLE, pdf_text(pdf))

    def test_neither_surface_states_it_when_totals_are_complete(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                page = self._respond(county, REPORT, "complete")
                pdf = self._respond(county, PDF, "complete")

                self.assertNotContains(page, "Tax totals unavailable")
                self.assertNotIn("Tax totals unavailable", pdf_text(pdf))


def history_rows(*years: int) -> list[dict]:
    """Assessment history rows for ``years``, newest first, as an adapter returns them."""
    return [
        {
            "tax_year": year,
            "assessed_value": Decimal("300000"),
            "appraised_value": Decimal("300000"),
            "market_value": Decimal("300000"),
            "increase_percent": None,
            "cap_status": None,
        }
        for year in sorted(years, reverse=True)
    ]


class HistoryAvailabilityNoticeTests(TestCase):
    """The page, CSV and PDF print the one assessment-history notice the dossier holds."""

    CASES = (
        (
            "no history",
            (),
            "Assessment history unavailable. Qualified property evidence remains available.",
        ),
        ("a missing year", (2025, 2023), "Assessment history gaps: 2024"),
        ("no gaps", (2025, 2024, 2023, 2022, 2021), ""),
    )

    def setUp(self):
        for county in COUNTIES:
            county.make_subject()

    def _get(self, county: County, route: str, years: tuple[int, ...]):
        with patch.object(county.adapter, "assessment_history", return_value=history_rows(*years)):
            return get(self.client, county, route, county.key, evidence_comps())

    def test_page_csv_and_pdf_print_the_same_notice(self):
        for county in COUNTIES:
            for case, years, notice in self.CASES:
                with self.subTest(county=county.slug, history=case):
                    page = self._get(county, REPORT, years)
                    csv_rows = list(
                        csv.DictReader(io.StringIO(self._get(county, CSV, years).content.decode()))
                    )
                    pdf = pdf_lines(self._get(county, PDF, years))

                    self.assertEqual(page.context["history_notice"], notice)
                    self.assertEqual(
                        {row["assessment_history_availability"] for row in csv_rows}, {notice}
                    )
                    printed = [line for line in pdf if line.startswith("Assessment history ")]
                    self.assertEqual(printed, [notice] if notice else [])
                    if notice:
                        self.assertContains(page, notice)


class ComparableShortfallWordingTests(TestCase):
    """The page, CSV and PDF state a comparable shortfall in the one wording."""

    GUIDANCE = "Lower the minimum score to find at least 3 comparables."
    CASES = (
        (2, "Comparable shortfall: only 2 comparables meet the minimum score of 70."),
        (0, "Comparable shortfall: no comparables meet the minimum score of 70."),
    )

    def setUp(self):
        for county in COUNTIES:
            county.make_subject()

    def test_page_csv_and_pdf_state_the_same_headline_and_guidance(self):
        for county in COUNTIES:
            for found, headline in self.CASES:
                with self.subTest(county=county.slug, found=found):
                    comps = evidence_comps()[:found]
                    page = get(self.client, county, REPORT, county.key, comps)
                    csv_doc = get(self.client, county, CSV, county.key, comps)
                    pdf = get(self.client, county, PDF, county.key, comps)

                    self.assertContains(page, headline)
                    self.assertContains(page, self.GUIDANCE)
                    cells = {
                        row["comparable_shortfall"]
                        for row in csv.DictReader(io.StringIO(csv_doc.content.decode()))
                    }
                    self.assertEqual(cells, {f"{headline} {self.GUIDANCE}"})
                    self.assertIn(headline, pdf_lines(pdf))
                    self.assertIn(self.GUIDANCE, pdf_lines(pdf))


class UnavailableWithoutAReasonTests(TestCase):
    """A property the county finds but will not serve always says why.

    When readiness gives no reason, every surface falls back to the one location sentence.
    """

    SENTENCE = "This property does not have location data required for similarity search."

    def setUp(self):
        for county in COUNTIES:
            county.make_subject()

    def test_every_route_states_the_location_sentence(self):
        no_reason = PropertyCapabilities(comparable_ready=False, report_ready=False)
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                with patch.object(county.adapter, "capabilities", return_value=no_reason):
                    comparables = get(self.client, county, COMPARABLES, county.key)
                    report = get(self.client, county, REPORT, county.key)
                    csv_response = get(self.client, county, CSV, county.key)
                    pdf = get(self.client, county, PDF, county.key)

                self.assertEqual(comparables.context["error"], self.SENTENCE)
                self.assertEqual(report.context["error"], self.SENTENCE)
                self.assertContains(comparables, self.SENTENCE)
                self.assertContains(report, self.SENTENCE)
                for response in (csv_response, pdf):
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.content.decode(), self.SENTENCE)
