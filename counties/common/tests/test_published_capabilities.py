"""Published outcomes stay independent of missing enrichment inputs."""

import csv
import io
import tempfile
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from counties.common.models import ImportOperation
from counties.common.tax_models import AssessmentHistory, PropertyJurisdictionExemption, TaxUnitRate
from counties.harris.adapter import adapter
from counties.harris.models import BuildingDetail, PropertyRecord


class PublishedCapabilitiesTests(TestCase):
    def setUp(self):
        ImportOperation.objects.create(
            county="harris",
            intent="full",
            status="published",
            requested_year=2026,
            publication_after={"property_source_year": 2026},
        )
        for index in range(4):
            prop = PropertyRecord.objects.create(
                account_number=f"P{index}",
                owner_name="Qualified owner",
                is_residential=True,
                is_data_ready=True,
                latitude=29.7 + index * 0.00001,
                longitude=-95.4,
                assessed_value=250000,
                building_area=1800,
            )
            BuildingDetail.objects.create(
                property=prop,
                account_number=prop.account_number,
                building_number=1,
                heat_area=1800,
                bedrooms=3,
                bathrooms=2,
            )

    def tax_rows(self, year=2025):
        PropertyJurisdictionExemption.objects.create(
            county="harris",
            account_number="P0",
            tax_year=year,
            tax_unit_code="A",
            taxable_value=250000,
        )
        TaxUnitRate.objects.create(
            county="harris", tax_year=year, tax_unit_code="A", adopted_rate=Decimal("0.01")
        )

    def test_current_property_year_does_not_borrow_older_complete_tax_inputs(self):
        self.tax_rows()
        report = self.client.get(reverse("protest_analysis", args=["P0"]))
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.context["subject"].tax_year, 2026)
        self.assertEqual(report.context["tax_impact"].tax_year, 2026)
        self.assertEqual(report.context["tax_impact"].completeness, "missing")
        self.assertContains(report, "Tax totals unavailable")
        self.assertContains(report, "Assessment history unavailable")
        exported = self.client.get(reverse("protest_analysis_export", args=["P0"]))
        rows = list(csv.DictReader(io.StringIO(exported.content.decode())))
        self.assertTrue(rows)
        self.assertTrue(all(row["current_tax_owed"] == "" for row in rows))
        pdf = self.client.get(reverse("protest_analysis_pdf", args=["P0"]))
        self.assertEqual(pdf.status_code, 200)
        self.assertIn(b"Tax totals unavailable", pdf.content)

    def test_partial_rates_withhold_totals_and_history_gaps_are_explicit(self):
        self.tax_rows(2026)
        PropertyJurisdictionExemption.objects.create(
            county="harris",
            account_number="P0",
            tax_year=2026,
            tax_unit_code="B",
            taxable_value=250000,
        )
        AssessmentHistory.objects.create(
            county="harris", account_number="P0", tax_year=2026, assessed_value=250000
        )
        AssessmentHistory.objects.create(
            county="harris", account_number="P0", tax_year=2024, assessed_value=230000
        )
        report = self.client.get(reverse("protest_analysis", args=["P0"]))
        self.assertEqual(report.context["tax_impact"].completeness, "partial")
        self.assertContains(report, "Tax totals unavailable")
        self.assertContains(report, "2025")
        self.assertContains(report, "Assessment history gaps")
        self.assertIsNone(report.context["assessment_history"][0]["increase_percent"])

    def test_failed_history_and_tax_operations_do_not_block_property_replacement(self):
        from counties.harris.etl_pipeline import run_harris_import
        from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings
        from counties.harris.etl_pipeline.tests.test_import_publication import harris_request

        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            for command, options in (
                (
                    "import_assessment_history",
                    {
                        "start_year": 2026,
                        "end_year": 2026,
                        "skip_download": True,
                        "skip_extract": True,
                    },
                ),
                ("import_hcad_jur_exempt", {"tax_year": 2026}),
            ):
                with self.assertRaises((CommandError, FileNotFoundError)):
                    call_command(command, stdout=io.StringIO(), **options)
            PropertyRecord.objects.exclude(account_number="P0").delete()
            result = run_harris_import(harris_request(root, key="P0"))
            self.assertTrue(result.wrote_data)
            self.assertEqual(ImportOperation.objects.filter(status="failed").count(), 2)
            self.assertEqual(PropertyRecord.objects.get().account_number, "P0")

    def test_unqualified_harris_subject_is_not_exposed_and_legacy_year_is_unknown(self):
        PropertyRecord.objects.filter(account_number="P0").update(is_data_ready=False)
        self.assertIsNone(adapter.get_subject("P0"))
        ImportOperation.objects.all().delete()
        ImportOperation.objects.create(
            county="harris", intent="legacy", status="published", requested_year=2026
        )
        self.tax_rows()
        subject = adapter.get_subject("P1")
        self.assertIsNone(subject.tax_year)
        result = adapter.tax_impact("P0", None, None)
        self.assertEqual(result.completeness, "missing")
        self.assertIn("source year", " ".join(result.warnings))
