"""Tests for counties/common/exports.py."""

from __future__ import annotations

from decimal import Decimal

from django.http import HttpResponse
from django.test import SimpleTestCase

from counties.common.analysis import (
    ProtestCompRow,
    ProtestEvidenceDossier,
    summarize_equity,
)
from counties.common.contracts import (
    Column,
    Comp,
    CountyProfile,
    SearchField,
    Subject,
)
from counties.common.exports import (
    ExportDocument,
    render_protest_csv,
    render_protest_export,
    render_protest_pdf,
    render_search_csv,
)


class ExportDocumentTests(SimpleTestCase):
    def test_to_response_converts_document_to_http_response(self):
        doc = ExportDocument(
            filename="sample.txt",
            content_type="text/plain",
            payload=b"Hello, world!",
        )
        response = doc.to_response()

        self.assertIsInstance(response, HttpResponse)
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="sample.txt"')
        self.assertEqual(response.content, b"Hello, world!")


class RenderExportsTests(SimpleTestCase):
    def setUp(self):
        self.profile = CountyProfile(
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
        self.subject = Subject(
            key="ACC001",
            address_line="100 Main St",
            assessed_value=Decimal("250000"),
            living_area=2000,
            tax_year=2026,
        )
        self.comp = Comp(
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
        self.equity = summarize_equity(self.subject, [self.comp])
        self.dossier = ProtestEvidenceDossier(
            subject=self.subject,
            comps=[self.comp],
            equity=self.equity,
            history=[],
            history_notice="",
            tax_impact=None,
            comp_rows=[
                ProtestCompRow(
                    comp=self.comp,
                    value_per_sqft=123.08,
                    delta=-1.92,
                    breakdown_summary="Grade 50%, Size 35%",
                )
            ],
            min_score=80.0,
            assessment_history_chart=None,
            ppsf_distribution_chart=None,
        )

    def test_render_search_csv_returns_export_document(self):
        columns = [
            Column(label="Address", key="address"),
            Column(label="Value", key="value", format="currency"),
        ]
        rows = [{"address": "123 Main St", "value": 250000}]
        doc = render_search_csv(columns, rows)

        self.assertIsInstance(doc, ExportDocument)
        self.assertEqual(doc.content_type, "text/csv")
        self.assertIn(b"Address,Value", doc.payload)
        self.assertIn(b"123 Main St,250000", doc.payload)

    def test_render_protest_csv_returns_export_document(self):
        doc = render_protest_csv(self.dossier)

        self.assertIsInstance(doc, ExportDocument)
        self.assertEqual(doc.content_type, "text/csv")
        self.assertEqual(doc.filename, "protest_analysis_ACC001.csv")
        self.assertIn(b"200 Oak Ave", doc.payload)
        self.assertIn(b"85.0", doc.payload)

    def test_render_protest_pdf_returns_export_document(self):
        doc = render_protest_pdf(self.profile, self.dossier)

        self.assertIsInstance(doc, ExportDocument)
        self.assertEqual(doc.content_type, "application/pdf")
        self.assertEqual(doc.filename, "protest_analysis_ACC001.pdf")
        self.assertTrue(doc.payload.startswith(b"%PDF-"))

    def test_render_protest_export_dispatches_correct_format(self):
        csv_doc = render_protest_export(self.profile, self.dossier, format="csv")
        self.assertEqual(csv_doc.content_type, "text/csv")

        pdf_doc = render_protest_export(self.profile, self.dossier, format="pdf")
        self.assertEqual(pdf_doc.content_type, "application/pdf")
