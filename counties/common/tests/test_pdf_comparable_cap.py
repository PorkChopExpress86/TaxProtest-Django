"""Ticket #114: the PDF states its comparable cap.

The PDF lists at most ten comparables on a one-page-oriented report. When the
dossier holds more, it says so with "Showing the 10 closest of N comparables", where N
is the dossier's comparable count, the same N the page header states. The checks are the
pure-Python stream assertions ADR-0017 asks for: the note text and position, the
number of comparable lines, and that the note adds one line without breaking the
page budget. Nothing renders the PDF or diffs pixels.

Expected values are literals (the cap is ten, a page holds 38 lines), never read back
from the renderer's own constants.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from counties.common.analysis import (
    ProtestCompRow,
    ProtestEvidenceDossier,
    summarize_equity,
)
from counties.common.contracts import Comp
from counties.common.exports import render_protest_pdf
from counties.common.tax_impact import unavailable_tax_impact
from counties.common.tests.test_dossier_comparable_rows import PROFILE, SUBJECT
from counties.common.tests.test_shared_route_characterization import (
    COUNTIES,
    PDF,
    REPORT,
    get,
    pdf_lines,
)

COMPARABLE_LINE = re.compile(r"^Comp \d\d Ln: score ")
NOTE_LINE = re.compile(r"^Showing .*comparables$")


def note(total: int) -> str:
    return f"Showing the 10 closest of {total} comparables"


def comps_best_first(total: int) -> list[Comp]:
    """``total`` comparables in display order, named ``Comp 00 Ln``, ``Comp 01 Ln``, and so on."""
    return [
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


def pdf_pages(payload: bytes) -> list[list[tuple[str, int]]]:
    """Each page of a PDF as its text lines with their baseline height, read from the streams."""
    text = payload.decode("latin-1")
    pages: list[list[tuple[str, int]]] = []
    for stream in re.findall(r"stream\n(.*?)\nendstream", text, flags=re.S):
        height = 0
        page: list[tuple[str, int]] = []
        for command in stream.split("\n"):
            if command.endswith(" Td") and command != "72 760 Td":
                height += int(command.split()[1])
            if match := re.fullmatch(r"\((.*)\) Tj", command):
                page.append((re.sub(r"\\([\\()])", r"\1", match.group(1)), 760 + height))
        pages.append(page)
    return pages


def hand_built_dossier(total: int, *, history_years: int = 0) -> ProtestEvidenceDossier:
    comps = comps_best_first(total)
    return ProtestEvidenceDossier(
        subject=SUBJECT,
        comps=comps,
        equity=summarize_equity(SUBJECT, comps),
        history=[{"tax_year": 2025 - year} for year in range(history_years)],
        history_notice="",
        tax_impact=unavailable_tax_impact(None, "Not computed."),
        comp_rows=[
            ProtestCompRow(comp=item, value_per_sqft=None, delta=None, breakdown_summary="")
            for item in comps
        ],
        min_score=70.0,
        assessment_history_chart=None,
        ppsf_distribution_chart=None,
    )


def texts(page: Sequence[tuple[str, int]]) -> list[str]:
    return [line for line, _ in page]


class RendererCapTests(SimpleTestCase):
    """The renderer applied to a hand-built dossier: the note, the cap and the order."""

    def lines(self, total: int, **kwargs) -> list[str]:
        payload = render_protest_pdf(PROFILE, hand_built_dossier(total, **kwargs)).payload
        return [line for page in pdf_pages(payload) for line in texts(page)]

    def test_more_comparables_than_the_cap_print_the_note_and_only_ten_comparables(self):
        for total in (11, 12, 25, 50):
            with self.subTest(comparables=total):
                lines = self.lines(total)

                self.assertEqual(lines.count(note(total)), 1)
                self.assertEqual(len([line for line in lines if NOTE_LINE.match(line)]), 1)
                listed = [line for line in lines if COMPARABLE_LINE.match(line)]
                self.assertEqual(len(listed), 10)
                self.assertEqual(
                    [line.split(":")[0] for line in listed],
                    [f"Comp {number:02d} Ln" for number in range(10)],
                )

    def test_the_note_sits_between_the_comparable_heading_and_the_first_comparable(self):
        lines = self.lines(12)

        heading = lines.index("Comparable Evidence")
        self.assertEqual(lines[heading + 1], note(12))
        self.assertRegex(lines[heading + 2], COMPARABLE_LINE)

    def test_comparables_up_to_the_cap_print_every_comparable_and_no_note(self):
        for total in (1, 3, 9, 10):
            with self.subTest(comparables=total):
                lines = self.lines(total)

                self.assertEqual(
                    len([line for line in lines if COMPARABLE_LINE.match(line)]), total
                )
                self.assertFalse([line for line in lines if "Showing" in line or "closest" in line])

    def test_the_note_is_the_only_line_the_cap_adds(self):
        self.assertEqual(len(self.lines(11)), len(self.lines(10)) + 1)
        self.assertEqual(len(self.lines(50)), len(self.lines(10)) + 1)

    def test_the_note_counts_the_dossier_comparables_not_the_rows_listed(self):
        self.assertIn(note(50), self.lines(50))
        self.assertNotIn(note(10), self.lines(50))

    def test_a_dense_report_with_the_note_still_fits_the_page_budget(self):
        payload = render_protest_pdf(PROFILE, hand_built_dossier(50, history_years=30)).payload

        pages = pdf_pages(payload)

        self.assertGreater(len(pages), 1)
        self.assertIn(f"/Count {len(pages)}".encode(), payload)
        for page in pages:
            self.assertLessEqual(len(page), 38)
        self.assertEqual(sum(1 for page in pages for line in texts(page) if line == note(50)), 1)
        self.assertEqual(
            sum(1 for page in pages for line in texts(page) if COMPARABLE_LINE.match(line)), 10
        )


class RouteCapTests(TestCase):
    """Seam A: for each real county, the PDF route states the cap and stays on its page."""

    def setUp(self):
        for county in COUNTIES:
            county.make_subject()

    def pdf(self, county, total: int):
        return get(self.client, county, PDF, county.key, comps_best_first(total)[::-1])

    def test_the_note_uses_the_comparable_count_the_page_states(self):
        for county in COUNTIES:
            for total in (11, 12, 50):
                with self.subTest(county=county.slug, comparables=total):
                    comps = comps_best_first(total)[::-1]
                    page = get(self.client, county, REPORT, county.key, comps)
                    lines = pdf_lines(self.pdf(county, total))

                    self.assertEqual(page.context["comparable_count"], total)
                    self.assertEqual(lines.count(note(page.context["comparable_count"])), 1)

    def test_up_to_ten_comparables_leave_no_note(self):
        for county in COUNTIES:
            for total in (0, 3, 10):
                with self.subTest(county=county.slug, comparables=total):
                    lines = pdf_lines(self.pdf(county, total))

                    self.assertEqual(len([ln for ln in lines if COMPARABLE_LINE.match(ln)]), total)
                    self.assertFalse([ln for ln in lines if "Showing" in ln or "closest" in ln])

    def test_the_note_follows_the_comparable_heading_directly(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                lines = pdf_lines(self.pdf(county, 12))

                heading = lines.index("Comparable Evidence")
                self.assertEqual(lines[heading + 1], note(12))
                self.assertRegex(lines[heading + 2], COMPARABLE_LINE)

    def test_fifty_comparables_stay_on_one_page_within_the_line_budget(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                pages = pdf_pages(self.pdf(county, 50).content)

                self.assertEqual(len(pages), 1)
                self.assertLessEqual(len(pages[0]), 38)
                # Baselines run down from 760pt; the last line keeps the one-inch margin.
                self.assertGreaterEqual(min(height for _, height in pages[0]), 72)

    def test_the_note_adds_one_line_and_no_page(self):
        for county in COUNTIES:
            with self.subTest(county=county.slug):
                at_cap = pdf_pages(self.pdf(county, 10).content)
                over_cap = pdf_pages(self.pdf(county, 11).content)

                self.assertEqual(len(over_cap), len(at_cap))
                self.assertEqual(len(over_cap[0]), len(at_cap[0]) + 1)
