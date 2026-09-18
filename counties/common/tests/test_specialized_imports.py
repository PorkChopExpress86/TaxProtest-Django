"""Specialized command seams share ownership without sharing source semantics."""

import hashlib
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.test import TransactionTestCase
from django.urls import reverse

from counties.brazos.coordinate_enrichment import (
    BrazosCoordinateEnrichment,
    CoordinateEnrichmentRequest,
)
from counties.brazos.tests.test_import_brazos_assessment_history import _entity_info_line
from counties.brazos.tests.test_import_brazos_tax_rates import FIXTURE_HTML, _mock_response
from counties.common.import_writers import WriterConflict
from counties.common.models import CountyWriter, ImportOperation
from counties.common.tax_models import AssessmentHistory, TaxUnitRate
from counties.harris.tests.test_import_hcad_jur_exempt import (
    EXEMPT_HEADER,
    RATE_HEADER,
    VALUE_HEADER,
)


class SpecializedImportTests(TransactionTestCase):
    def test_later_history_write_failure_retains_committed_year_without_claiming_completion(self):
        with (
            tempfile.TemporaryDirectory() as root,
            self.settings(
                BCAD_DOWNLOAD_DIR=str(Path(root) / "download"),
                BCAD_EXTRACT_DIR=str(Path(root) / "extract"),
            ),
        ):
            downloads = Path(root) / "download"
            downloads.mkdir()
            for year in (2025, 2026):
                (downloads / f"bcad_certified_{year}.zip").write_bytes(b"retained")
                extracted = Path(root) / "extract" / str(year)
                extracted.mkdir(parents=True)
                (extracted / "APPRAISAL_ENTITY_INFO.TXT").write_text(
                    _entity_info_line("000000010013", str(year), "G1") + "\n"
                )

            def fail_later_write(execute, sql, params, many, context):
                if sql.startswith('DELETE FROM "data_assessmenthistory"') and 2026 in params:
                    raise DatabaseError("Second year database write failed")
                return execute(sql, params, many, context)

            with connection.execute_wrapper(fail_later_write), self.assertRaises(DatabaseError):
                call_command(
                    "import_brazos_assessment_history",
                    start_year=2025,
                    end_year=2026,
                    skip_download=True,
                    skip_extract=True,
                    all_accounts=True,
                )
            self.assertTrue(
                AssessmentHistory.objects.filter(county="brazos", tax_year=2025).exists()
            )
            self.assertFalse(
                AssessmentHistory.objects.filter(county="brazos", tax_year=2026).exists()
            )
            user = get_user_model().objects.create_superuser("viewer", password="test")
            self.client.force_login(user)
            operation = ImportOperation.objects.get(county="brazos", intent="assessment_history")
            detail = self.client.get(
                reverse("admin:data_importoperation_change", args=[operation.pk])
            )
            for text in ("failed", "Second year database write failed", "rows_by_year", "2025"):
                self.assertContains(detail, text)
            self.assertNotContains(detail, "completed_with_warnings")

    def test_history_source_year_evidence_does_not_invent_requested_year(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "extracted" / "2026" / "Real_acct_owner" / "real_acct.txt"
            source.parent.mkdir(parents=True)
            source.write_text("acct\tyr\tassessed_val\nPRIOR\t2025\t250000\n")
            call_command(
                "import_assessment_history",
                start_year=2026,
                end_year=2026,
                skip_download=True,
                skip_extract=True,
                extract_root=str(Path(root) / "extracted"),
                download_root=str(Path(root) / "downloads"),
            )
            self.assertTrue(
                AssessmentHistory.objects.filter(county="harris", tax_year=2025).exists()
            )
            operation = ImportOperation.objects.get(county="harris", intent="assessment_history")
            self.assertEqual(operation.evidence["observed_source_years"], [2025])
            self.assertEqual(operation.evidence["rows_by_tax_year"], {"2025": 1})
            self.assertIsNone(operation.evidence["sources"][0]["source_year"])

    def test_missing_jurisdiction_description_warning_is_retained_in_admin(self):
        with tempfile.TemporaryDirectory() as root:
            for filename, header, row in (
                (
                    "jur_tax_dist_exempt_value_rate.txt",
                    RATE_HEADER,
                    "Real\t001\tHOUSTON ISD\tRES\t0.868300\t0.878300\t0\t0",
                ),
                ("jur_value.txt", VALUE_HEADER, "P100\t001\tI\t1\t250000\t250000"),
                ("jur_exempt.txt", EXEMPT_HEADER, "P100\t001\tNONE\t"),
            ):
                (Path(root) / filename).write_text(header + "\n" + row + "\n")
            call_command("import_hcad_jur_exempt", tax_year=2026, path=root, all_accounts=True)
            user = get_user_model().objects.create_superuser("viewer", password="test")
            self.client.force_login(user)
            operation = ImportOperation.objects.get(intent="jurisdictions_exemptions_rates")
            detail = self.client.get(
                reverse("admin:data_importoperation_change", args=[operation.pk])
            )
            self.assertContains(detail, "jur_exemption_dscr.txt missing")

    def test_coordinate_apply_rejects_competing_writer_before_acquisition(self):
        owner = ImportOperation.objects.create(county="brazos", intent="interrupted import")
        CountyWriter.objects.create(county="brazos", operation=owner)
        with self.assertRaises(WriterConflict) as rejected:
            BrazosCoordinateEnrichment().apply(
                CoordinateEnrichmentRequest(target_year=2026), minimum_match_rate=0.5
            )
        self.assertEqual(rejected.exception.operation_id, owner.pk)

    def test_specialized_commands_reject_reserved_county_before_source_work(self):
        for county, commands in (
            (
                "harris",
                ["import_assessment_history", "import_hcad_jur_exempt", "reconcile_property_data"],
            ),
            ("brazos", ["import_brazos_assessment_history", "import_brazos_tax_rates"]),
        ):
            owner = ImportOperation.objects.create(county=county, intent="interrupted import")
            CountyWriter.objects.create(county=county, operation=owner)
            with tempfile.TemporaryDirectory() as root:
                for command in commands:
                    options = {}
                    if "assessment_history" in command:
                        options = dict(
                            start_year=2026, end_year=2026, skip_download=True, skip_extract=True
                        )
                    elif command == "import_hcad_jur_exempt":
                        options = dict(path=root, tax_year=2026)
                    with (
                        self.subTest(command=command),
                        patch("requests.get", return_value=_mock_response(FIXTURE_HTML)),
                        self.assertRaises(WriterConflict) as rejected,
                    ):
                        call_command(command, **options)
                    self.assertEqual(rejected.exception.operation_id, owner.pk)

    def test_tax_command_records_actual_source_and_target_year_and_preserves_harris(self):
        owner = ImportOperation.objects.create(county="harris", intent="interrupted import")
        CountyWriter.objects.create(county="harris", operation=owner)
        TaxUnitRate.objects.create(
            county="harris", tax_year=2026, tax_unit_code="G1", adopted_rate=Decimal("0.01")
        )
        with (
            tempfile.TemporaryDirectory() as root,
            self.settings(BCAD_DOWNLOAD_DIR=root),
            patch("requests.get", return_value=_mock_response(FIXTURE_HTML)),
        ):
            call_command("import_brazos_tax_rates", year=2026, actor="county operator")
        self.assertEqual(TaxUnitRate.objects.get(county="harris").adopted_rate, Decimal("0.01"))
        self.assertEqual(
            TaxUnitRate.objects.get(county="brazos", tax_unit_code="G1").adopted_rate,
            Decimal("0.004197"),
        )
        user = get_user_model().objects.create_superuser("viewer", password="test")
        self.client.force_login(user)
        operation = ImportOperation.objects.get(county="brazos", intent="tax_rates")
        detail = self.client.get(reverse("admin:data_importoperation_change", args=[operation.pk]))
        for text in (
            "county operator",
            "source_year",
            "2025",
            "target_year",
            "2026",
            "rows_upserted",
            "2",
            hashlib.sha256(FIXTURE_HTML.encode()).hexdigest(),
        ):
            self.assertContains(detail, text)

    def test_history_command_audits_exact_source_and_keeps_other_county_history(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "extracted" / "2026" / "Real_acct_owner" / "real_acct.txt"
            source.parent.mkdir(parents=True)
            source.write_text("acct\tyr\tassessed_val\nSAME\t2026\t250000\n")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            AssessmentHistory.objects.create(
                county="brazos", account_number="SAME", tax_year=2026, assessed_value=123
            )
            call_command(
                "import_assessment_history",
                start_year=2026,
                end_year=2026,
                skip_download=True,
                skip_extract=True,
                extract_root=str(Path(root) / "extracted"),
                download_root=str(Path(root) / "downloads"),
                actor="history operator",
            )
            self.assertEqual(
                AssessmentHistory.objects.get(county="brazos").assessed_value, Decimal("123")
            )
            self.assertEqual(
                AssessmentHistory.objects.get(county="harris").assessed_value, Decimal("250000")
            )
            user = get_user_model().objects.create_superuser("viewer", password="test")
            self.client.force_login(user)
            operation = ImportOperation.objects.get(county="harris", intent="assessment_history")
            detail = self.client.get(
                reverse("admin:data_importoperation_change", args=[operation.pk])
            )
            for text in ("history operator", digest, "records_loaded", "1", "2026"):
                self.assertContains(detail, text)
