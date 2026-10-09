"""Tax completeness is a closed set with one "may totals be shown" answer (spec #106, ticket #113).

A county adapter whose ``tax_impact`` returns ``None`` is reported by the dossier as an
unavailable tax impact, so the page, CSV and PDF render the same missing-completeness
shape as any other unavailable result and the CSV header never changes.
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from counties.common.analysis import build_protest_dossier
from counties.common.contracts import Subject
from counties.common.exports import render_protest_csv
from counties.common.tax_impact import TaxCompleteness, TaxImpactResult, unavailable_tax_impact
from counties.common.tests.test_analysis import FakeAdapter
from counties.common.tests.test_shared_route_characterization import (
    COUNTIES,
    CSV,
    CSV_COMP_COLUMNS,
    CSV_HEADER,
    CSV_TAIL,
    PAGE_TAX_BADGE,
    PAGE_TAX_UNAVAILABLE,
    PDF,
    PDF_COMPS,
    PDF_HEAD,
    PDF_TAX_UNAVAILABLE,
    REPORT,
    County,
    csv_text,
    evidence_comps,
    get,
    pdf_lines,
)

#: The sentence a null tax impact becomes. New wording introduced by ticket #113.
NO_TAX_IMPACT_REASON = "No tax impact estimate is available from this county."


def _tax_impact(completeness: str) -> TaxImpactResult:
    return TaxImpactResult(
        tax_year=2026,
        current_tax_owed=Decimal("100.00"),
        median_tax_owed=Decimal("80.00"),
        estimated_savings=Decimal("20.00"),
        effective_rate=Decimal("0.021000"),
        current_assessed_value=Decimal("300000.00"),
        taxable_value_used=Decimal("300000.00"),
        completeness=completeness,
        warnings=[],
        exemptions_summary=[],
        per_unit_breakdown=[],
    )


class TaxImpactAdapter(FakeAdapter):
    """The fake county, with the tax impact it was handed instead of ``None``."""

    def __init__(self, tax_impact, **kwargs):
        super().__init__(**kwargs)
        self._tax_impact = tax_impact

    def tax_impact(self, key, tax_year, median_assessed_value):
        return self._tax_impact


SUBJECT = Subject(
    key="1",
    address_line="123 Main",
    has_location=True,
    assessed_value=Decimal("300000"),
    living_area=2000,
    tax_year=2026,
)


class NullTaxImpactBuilderTests(SimpleTestCase):
    def test_a_county_with_no_tax_impact_yields_an_unavailable_result_with_a_reason(self):
        outcome = build_protest_dossier(FakeAdapter(subject=SUBJECT), "1")

        tax_impact = outcome.dossier.tax_impact
        self.assertEqual(tax_impact.completeness, "missing")
        self.assertEqual(tax_impact.tax_year, 2026)
        self.assertEqual(tax_impact.warnings, [NO_TAX_IMPACT_REASON])
        self.assertEqual(tax_impact.per_unit_breakdown, [])
        self.assertFalse(tax_impact.may_show_totals)

    def test_a_tax_impact_the_county_computed_is_passed_through_untouched(self):
        computed = _tax_impact("complete")

        outcome = build_protest_dossier(TaxImpactAdapter(computed, subject=SUBJECT), "1")

        self.assertIs(outcome.dossier.tax_impact, computed)

    def test_the_csv_header_is_the_same_whether_or_not_the_county_has_a_tax_impact(self):
        for tax_impact in (None, _tax_impact("complete")):
            with self.subTest(has_tax_impact=tax_impact is not None):
                dossier = build_protest_dossier(
                    TaxImpactAdapter(tax_impact, subject=SUBJECT), "1"
                ).dossier

                header = render_protest_csv(dossier).payload.decode().splitlines()[0]

                self.assertEqual(header, CSV_HEADER)


class TaxCompletenessTypeTests(SimpleTestCase):
    def test_the_set_is_missing_partial_and_complete(self):
        self.assertEqual(
            {member.value for member in TaxCompleteness}, {"missing", "partial", "complete"}
        )

    def test_only_complete_may_show_totals(self):
        for completeness in ("missing", "partial", "complete"):
            with self.subTest(completeness=completeness):
                self.assertEqual(
                    _tax_impact(completeness).may_show_totals, completeness == "complete"
                )

    def test_a_result_built_from_the_spelling_holds_the_member(self):
        self.assertIs(_tax_impact("partial").completeness, TaxCompleteness.PARTIAL)

    def test_a_completeness_outside_the_set_is_rejected(self):
        for completeness in ("completed", "Complete", "", None):
            with self.subTest(completeness=completeness), self.assertRaises(ValueError):
                _tax_impact(completeness)

    def test_every_unavailable_result_is_missing(self):
        self.assertIs(unavailable_tax_impact(2025, "why").completeness, TaxCompleteness.MISSING)


class NullTaxImpactRouteTests(TestCase):
    """The same routes and county fixtures as the characterization pins, with a null result.

    A null result must read exactly like the county's own unavailable result: only the
    tax year and the reason differ.
    """

    def setUp(self):
        for county in COUNTIES:
            county.make_subject()

    def _respond(self, county: County, route: str):
        with patch.object(county.adapter, "tax_impact", return_value=None):
            return get(self.client, county, route, county.key, evidence_comps())

    def _tax_year(self, county: County) -> str:
        """The subject's source year as the CSV prints it; Harris's fixture records none."""
        return "" if county.slug == "harris" else "2025"

    def test_page_shows_the_missing_badge_the_reason_and_no_totals(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                response = self._respond(county, REPORT)

                self.assertEqual(response.status_code, 200)
                badge = re.search(
                    r'Tax Impact</h2>\s*<span class="text-xs px-2 py-1 rounded-full '
                    r'([^"]+)">\s*(\w+)\s*</span>',
                    response.content.decode(),
                )
                self.assertIsNotNone(badge)
                self.assertEqual(badge.groups(), PAGE_TAX_BADGE["missing"])
                self.assertContains(response, PAGE_TAX_UNAVAILABLE)
                self.assertContains(response, f"⚠ {NO_TAX_IMPACT_REASON}")
                self.assertNotContains(response, "Current Taxes Owed")

    def test_csv_keeps_its_header_and_prints_the_missing_row(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                response = self._respond(county, CSV)

                tax = f"{self._tax_year(county)},missing,,,,{NO_TAX_IMPACT_REASON}"
                tail = CSV_TAIL[county.slug]
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.content.decode(),
                    csv_text(*(f"{comp},{tax},{tail}" for comp in CSV_COMP_COLUMNS)),
                )

    def test_pdf_prints_the_tax_section_as_unavailable(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                response = self._respond(county, PDF)

                year = self._tax_year(county) or "-"
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    pdf_lines(response),
                    [
                        *PDF_HEAD[county.slug],
                        *PDF_COMPS,
                        "",
                        "Tax Impact (Estimated)",
                        f"Tax Year Used: {year} (missing)",
                        PDF_TAX_UNAVAILABLE,
                        f"Warnings: {NO_TAX_IMPACT_REASON}",
                    ],
                )


ROOT = Path(__file__).resolve().parents[3]
COMPLETENESS_SPELLINGS = frozenset(member.value for member in TaxCompleteness)


def compared_completeness_spellings(source: str) -> list[str]:
    """Completeness spellings that a comparison in Python source tests a value against."""
    return [
        constant.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Compare)
        for constant in ast.walk(node)
        if isinstance(constant, ast.Constant) and constant.value in COMPLETENESS_SPELLINGS
    ]


def template_completeness_spellings(source: str) -> list[str]:
    """Completeness spellings that a template comparison tests a value against."""
    spellings = "|".join(sorted(COMPLETENESS_SPELLINGS))
    return re.findall(rf"[!=]=\s*['\"]({spellings})['\"]", source)


class NoBareCompletenessComparisonTests(SimpleTestCase):
    """No surface decides what completeness means by comparing its spelling."""

    def test_the_scanners_find_a_comparison(self):
        self.assertEqual(
            compared_completeness_spellings("ok = tax.completeness == 'complete'"), ["complete"]
        )
        self.assertEqual(
            template_completeness_spellings("{% if tax.completeness == 'partial' %}"), ["partial"]
        )

    def test_shared_python_surfaces_compare_no_completeness_spelling(self):
        for name in ("analysis.py", "exports.py", "views.py", "tax_evaluation.py"):
            with self.subTest(module=name):
                source = (ROOT / "counties" / "common" / name).read_text(encoding="utf-8")
                self.assertEqual(compared_completeness_spellings(source), [])

    def test_shared_templates_compare_no_completeness_spelling(self):
        for template in sorted((ROOT / "templates" / "counties").rglob("*.html")):
            with self.subTest(template=template.name):
                source = template.read_text(encoding="utf-8")
                self.assertEqual(template_completeness_spellings(source), [])
