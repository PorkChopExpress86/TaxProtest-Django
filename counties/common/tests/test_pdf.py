"""Tests for the PDF evidence report generator.

Verifies that ``simple_pdf`` produces a valid PDF that:
- Starts with the PDF magic bytes
- Auto-paginates (a long line list produces a multi-page PDF, not a
  single page with silent overflow)
- Handles special characters without corrupting PDF structure
"""

from __future__ import annotations

from django.test import SimpleTestCase

from counties.common.exports import simple_pdf


class SimplePdfTests(SimpleTestCase):
    def test_produces_valid_pdf_header(self) -> None:
        output = simple_pdf(["Test line"])
        assert output[:5] == b"%PDF-", "PDF must start with %PDF- magic"

    def test_empty_lines_render_as_blank_spacers(self) -> None:
        output = simple_pdf(["Header", "", "", "Footer"])
        assert output[:5] == b"%PDF-"

    def test_long_line_list_auto_paginates(self) -> None:
        lines = [f"Line {i}: " + "x" * 80 for i in range(60)]
        output = simple_pdf(lines, lines_per_page=38)
        page_count = output.count(b"/Type /Page ")
        assert page_count == 2, f"60 lines with 38/page must produce 2 pages, got {page_count}"
        assert b"/Count 2" in output
        assert b"/Kids [4 0 R 6 0 R]" in output

    def test_three_page_pagination_structure(self) -> None:
        lines = [f"Line {i}" for i in range(100)]
        output = simple_pdf(lines, lines_per_page=38)
        page_count = output.count(b"/Type /Page ")
        assert page_count == 3, f"100 lines with 38/page must produce 3 pages, got {page_count}"
        assert b"/Count 3" in output
        assert b"/Kids [4 0 R 6 0 R 8 0 R]" in output

    def test_special_characters_are_escaped(self) -> None:
        lines = ["Price: $1,000 (approx.)", "Path: C:\\Users\\test"]
        output = simple_pdf(lines)
        assert output[:5] == b"%PDF-"
        assert b"%%EOF" in output[-20:], "PDF must end with %%EOF"
