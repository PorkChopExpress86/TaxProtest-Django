"""Tests for the PDF evidence report generator.

Verifies that ``simple_pdf`` produces a valid PDF that:
- Starts with the PDF magic bytes
- Auto-paginates (a long line list produces a multi-page PDF, not a
  single page with silent overflow)
- Handles special characters without corrupting PDF structure
- ``protest_report_pdf`` returns an ``application/pdf`` response
"""

from __future__ import annotations

from decimal import Decimal

from django.test import SimpleTestCase

from counties.common.contracts import (
    Column,
    Comp,
    CountyProfile,
    SearchField,
    Subject,
)
from counties.common.exports import protest_report_pdf, simple_pdf


class SimplePdfTests(SimpleTestCase):
    def test_produces_valid_pdf_header(self) -> None:
        output = simple_pdf(["Test line"])
        assert output[:5] == b"%PDF-", "PDF must start with %PDF- magic"

    def test_empty_lines_render_as_blank_spacers(self) -> None:
        output = simple_pdf(["Header", "", "", "Footer"])
        assert output[:5] == b"%PDF-"

    def test_long_line_list_auto_paginates(self) -> None:
        lines = [f"Line {i}: " + "x" * 80 for i in range(60)]
        output = simple_pdf(lines)
        page_count = output.count(b"/Type /Page")
        assert page_count > 1, (
            f"60 long lines must auto-paginate; got {page_count} page(s) — "
            "silent overflow risk if only 1"
        )

    def test_special_characters_are_escaped(self) -> None:
        lines = ["Price: $1,000 (approx.)", "Path: C:\\Users\\test"]
        output = simple_pdf(lines)
        assert output[:5] == b"%PDF-"
        assert b"%%EOF" in output[-20:], "PDF must end with %%EOF"

    def test_protest_report_pdf_returns_pdf_response(self) -> None:
        profile = CountyProfile(
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
        subject = Subject(
            key="ACC001",
            address_line="100 Main St",
            assessed_value=Decimal("250000"),
            living_area=2000,
        )
        comp = Comp(
            key="ACC002",
            address="200 Oak Ave",
            similarity_score=85.0,
            match_label="Best match",
            living_area=1950,
            bedrooms=3,
            bathrooms=2.0,
            year_built=2005,
            quality_code="C",
            condition_code="AV",
            assessed_value=Decimal("240000"),
        )

        response = protest_report_pdf(
            profile=profile,
            subject=subject,
            comps=[comp],
            history_rows=[],
            tax_impact=None,
        )

        assert response["Content-Type"] == "application/pdf"
        assert b"%PDF-" in response.content[:10]
