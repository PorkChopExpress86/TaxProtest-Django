"""Ticket #111: comparable rows, the comparable count and the request parameters.

The Protest evidence dossier computes each comparable's price per square foot, delta
against the subject and score-breakdown summary once. The page, CSV and PDF only read
those rows. The builder tests use the fake adapter; the renderer tests hand the
renderers a dossier whose rows differ from what the comparables would recompute to,
so a surface that recomputed would show the wrong number; the route tests drive both
real counties.

Expected values are literals worked out by hand, not recomputed with the code under test.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from counties.common.analysis import (
    ProtestCompRow,
    ProtestEvidenceDossier,
    build_protest_dossier,
    summarize_equity,
)
from counties.common.contracts import (
    Column,
    Comp,
    CountyProfile,
    ScoreComponent,
    SearchField,
    Subject,
)
from counties.common.exports import render_protest_csv, render_protest_pdf
from counties.common.tests.test_analysis import FakeAdapter
from counties.common.tests.test_shared_route_characterization import (
    COUNTIES,
    CSV,
    PDF,
    REPORT,
    get,
    pdf_lines,
)

SUBJECT = Subject(
    key="1",
    address_line="123 Main",
    assessed_value=Decimal("300000"),
    living_area=2000,
    tax_year=2026,
)


PROFILE = CountyProfile(
    slug="test",
    display_name="Test County",
    district_abbr="TCAD",
    district_name="Test County Appraisal District",
    key_label="Account",
    url_prefix="",
    url_name_prefix="",
    search_fields=[SearchField(name="q", label="Search")],
    search_columns=[Column(label="Address", key="address")],
    comp_columns=[Column(label="Address", key="address")],
)


def comp(key: str, score: float, assessed: str | None, area: float | None, **extra) -> Comp:
    return Comp(
        key=key,
        address=f"{key} Main St",
        similarity_score=score,
        match_label="Similar",
        assessed_value=Decimal(assessed) if assessed else None,
        living_area=area,
        **extra,
    )


class BuildProtestDossierRowsTests(SimpleTestCase):
    """Seam B: the dossier carries the rows, the count and the request parameters."""

    def setUp(self):
        self.comps = [
            comp(
                "A",
                90.0,
                "250000",
                2000,
                score_breakdown=[
                    ScoreComponent("living_area", "Living area", 24.0, 0.85, 20.4),
                    ScoreComponent("year_built", "Year built", 8.0, None, None),
                ],
            ),
            comp("B", 80.0, "330000", 2000),
            comp("C", 75.0, None, None),
        ]

    def dossier(self, **kwargs):
        outcome = build_protest_dossier(
            FakeAdapter(subject=SUBJECT, comps=self.comps), "1", **kwargs
        )
        assert outcome.dossier is not None
        return outcome.dossier

    def test_rows_hold_price_per_sqft_delta_and_breakdown_summary(self):
        rows = {row.comp.key: row for row in self.dossier().comp_rows}

        # Subject $/sqft is 300000 / 2000 = 150.
        self.assertEqual(rows["A"].value_per_sqft, 125.0)
        self.assertEqual(rows["A"].delta, -25.0)
        self.assertEqual(rows["A"].breakdown_summary, "Living area: 20.4/24.0")
        self.assertEqual(rows["B"].value_per_sqft, 165.0)
        self.assertEqual(rows["B"].delta, 15.0)
        self.assertEqual(rows["B"].breakdown_summary, "")

    def test_a_comparable_without_value_or_area_has_no_price_or_delta(self):
        row = {row.comp.key: row for row in self.dossier().comp_rows}["C"]

        self.assertIsNone(row.value_per_sqft)
        self.assertIsNone(row.delta)

    def test_rows_follow_the_display_order(self):
        self.assertEqual([row.comp.key for row in self.dossier().comp_rows], ["A", "B", "C"])

    def test_the_comparable_count_is_the_number_of_comparables_found(self):
        self.assertEqual(self.dossier().comparable_count, 3)

    def test_the_comparable_count_follows_the_minimum_score(self):
        self.assertEqual(self.dossier(min_score="85").comparable_count, 1)

    def test_the_comparable_count_is_zero_when_nothing_qualifies(self):
        self.assertEqual(self.dossier(min_score="99").comparable_count, 0)

    def test_the_dossier_records_the_request_parameters_it_used(self):
        dossier = self.dossier(min_score="85")

        self.assertEqual(dossier.min_score, 85.0)
        self.assertEqual(dossier.max_distance, 10.0)
        self.assertEqual(dossier.max_results, 50)

    def test_the_recorded_parameters_are_the_ones_the_adapter_was_asked_for(self):
        adapter = FakeAdapter(subject=SUBJECT, comps=self.comps)

        with patch.object(adapter, "find_comps", return_value=[]) as find_comps:
            outcome = build_protest_dossier(adapter, "1", min_score="85")

        dossier = outcome.dossier
        assert dossier is not None
        find_comps.assert_called_once_with(
            "1",
            max_distance_miles=dossier.max_distance,
            max_results=dossier.max_results,
            min_score=dossier.min_score,
        )


class RendererRowsTests(SimpleTestCase):
    """The CSV and PDF print the rows the dossier holds; they compute none of their own.

    The row values below are deliberately not what the comparable would recompute to
    (it would give 125.00 and -25.00, and no breakdown).
    """

    def setUp(self):
        self.comp = comp("A", 90.0, "250000", 2000)
        self.dossier = ProtestEvidenceDossier(
            subject=SUBJECT,
            comps=[self.comp],
            equity=summarize_equity(SUBJECT, [self.comp]),
            history=[],
            history_notice="",
            tax_impact=None,
            comp_rows=[
                ProtestCompRow(
                    comp=self.comp,
                    value_per_sqft=999.99,
                    delta=-1.5,
                    breakdown_summary="Stub: 1/2",
                )
            ],
            min_score=70.0,
            assessment_history_chart=None,
            ppsf_distribution_chart=None,
        )

    def test_csv_prints_the_row_values(self):
        payload = render_protest_csv(self.dossier).payload.decode()

        row = next(csv.DictReader(io.StringIO(payload)))
        self.assertEqual(row["value_per_sqft"], "999.99")
        self.assertEqual(row["delta_vs_subject_per_sqft"], "-1.50")
        self.assertEqual(row["score_breakdown"], "Stub: 1/2")

    def test_pdf_prints_the_row_price_per_square_foot(self):
        payload = render_protest_pdf(PROFILE, self.dossier).payload.decode("latin-1")

        self.assertIn("(A Main St: score 90.0, $999.99/sqft) Tj", payload)

    def test_comparable_count_follows_the_rows(self):
        self.assertEqual(self.dossier.comparable_count, 1)

    def test_the_request_parameters_default_to_the_protest_report_bounds(self):
        self.assertEqual(self.dossier.max_distance, 10.0)
        self.assertEqual(self.dossier.max_results, 50)


def rows_comps() -> list[Comp]:
    """Two comparables with worked $/sqft against the report-ready fixture subjects.

    The subject's value per square foot differs by county, so the expected deltas are
    read from the page's own subject figure in the tests, never recomputed from a comp.
    """
    return [
        comp(
            "R1",
            88.0,
            "262500",
            2100,
            score_breakdown=[ScoreComponent("living_area", "Living area", 24.0, 0.5, 12.0)],
        ),
        comp("R2", 77.0, "180000", 1800),
    ]


class SurfacesAgreeOnTheRowsTests(TestCase):
    """Seam A: for each real county the page, CSV and PDF show the same row values."""

    def setUp(self):
        for county in COUNTIES:
            county.make_subject()

    def test_ppsf_delta_and_breakdown_match_on_page_csv_and_pdf(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                comps = rows_comps()
                page = get(self.client, county, REPORT, county.key, comps)
                csv_response = get(self.client, county, CSV, county.key, comps)
                pdf = get(self.client, county, PDF, county.key, comps)

                rows = {row.comp.key: row for row in page.context["comp_rows"]}
                # 262500 / 2100 and 180000 / 1800.
                self.assertEqual(rows["R1"].value_per_sqft, 125.0)
                self.assertEqual(rows["R2"].value_per_sqft, 100.0)
                self.assertEqual(rows["R1"].breakdown_summary, "Living area: 12.0/24.0")
                subject_ppsf = page.context["equity"].subject_value_per_sqft
                self.assertEqual(rows["R1"].delta, 125.0 - subject_ppsf)
                self.assertEqual(rows["R2"].delta, 100.0 - subject_ppsf)

                csv_rows = {
                    row["address"]: row
                    for row in csv.DictReader(io.StringIO(csv_response.content.decode()))
                }
                for key, row in rows.items():
                    printed = csv_rows[f"{key} Main St"]
                    self.assertEqual(printed["value_per_sqft"], f"{row.value_per_sqft:.2f}")
                    self.assertEqual(printed["delta_vs_subject_per_sqft"], f"{row.delta:.2f}")
                    self.assertEqual(printed["score_breakdown"], row.breakdown_summary)

                lines = pdf_lines(pdf)
                self.assertIn("R1 Main St: score 88.0, $125.00/sqft", lines)
                self.assertIn("R2 Main St: score 77.0, $100.00/sqft", lines)
                body = page.content.decode()
                self.assertIn("$125.00", body)
                self.assertIn("$100.00", body)


class ComparableCountOnThePageTests(TestCase):
    """The report page states the dossier's comparable count in its comparables header."""

    def setUp(self):
        for county in COUNTIES:
            county.make_subject()

    def page(self, county, total: int):
        comps = [comp(f"N{n:02d}", 99.0 - n, "250000", 2000) for n in range(total)]
        return get(self.client, county, REPORT, county.key, comps)

    def test_header_counts_the_comparables(self):
        for county in COUNTIES:
            for total, phrase in ((12, "12 properties"), (1, "1 property")):
                with self.subTest(county=county.slug, comparables=total):
                    page = self.page(county, total)

                    text = " ".join(re.sub(r"<[^>]+>", " ", page.content.decode()).split())
                    self.assertIn(f"{phrase} with similarity score", text)

    def test_the_page_hands_the_template_the_dossier_count(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                self.assertEqual(self.page(county, 12).context["comparable_count"], 12)
