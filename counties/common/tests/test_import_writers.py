"""County import coordination through real operations and PostgreSQL connections."""

import tempfile
from pathlib import Path
from threading import Event, Thread
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import connections
from django.test import TransactionTestCase
from django.urls import reverse

from counties.brazos.annual_refresh import RefreshOptions
from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
)
from counties.brazos.tests.test_property_import import _stage_complete_pacs_export
from counties.common.import_writers import WriterConflict
from counties.common.models import CountyWriter, ImportOperation
from counties.harris.etl_pipeline import (
    ExtractedSourceRetention,
    HarrisAcquisitionMode,
    HarrisApply,
    HarrisExtractionMode,
    HarrisImportRequest,
    HarrisPreview,
    run_harris_import,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.tests.test_harris_import import (
    _runtime_settings,
    _write_property_source,
)


class ImportWriterTests(TransactionTestCase):
    def test_competing_acquisition_always_reports_a_durable_owner(self):
        acquired, release = Event(), Event()
        results, failures = [], []

        def pause_database_acquisition(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if "pg_try_advisory_lock" in sql:
                acquired.set()
                if not release.wait(15):
                    raise TimeoutError("Test did not release database acquisition")
            return result

        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            _write_property_source(root)
            request = HarrisImportRequest(
                plan=HarrisImportPlan.from_legacy_scope("property-only"),
                acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                load=HarrisPreview(),
            )

            def run():
                try:
                    with connections["default"].execute_wrapper(pause_database_acquisition):
                        results.append(run_harris_import(request))
                except Exception as exc:
                    failures.append(exc)
                finally:
                    connections.close_all()

            worker = Thread(target=run)
            worker.start()
            try:
                self.assertTrue(acquired.wait(15))
                with self.assertRaises(WriterConflict) as rejected:
                    run_harris_import(request)
                owner = rejected.exception.operation_id
                self.assertIsInstance(owner, UUID)
            finally:
                release.set()
                worker.join(20)
            self.assertEqual(failures, [])
            self.assertEqual(results[0].operation_id, owner)

    def test_definitively_failed_writer_releases_before_retry(self):
        with (
            tempfile.TemporaryDirectory() as root,
            self.settings(
                BCAD_DOWNLOAD_DIR=str(Path(root) / "downloads"),
                BCAD_EXTRACT_DIR=str(Path(root) / "extracted"),
            ),
        ):
            source = Path(root) / "extracted" / "2026" / "APPRAISAL_INFO.TXT"
            _stage_complete_pacs_export(Path(root), 2026)
            original = source.read_text(encoding="utf-8")
            source.write_text("malformed", encoding="utf-8")
            with self.assertRaises(CommandError):
                BrazosPropertyImport(CadRefreshStage(), None).run(
                    PropertyImportRequest(
                        mode=PropertyImportMode.CAD_RECOVERY,
                        options=RefreshOptions(
                            tax_year=2026, skip_download=True, skip_extract=True
                        ),
                    )
                )
            source.write_text(original, encoding="utf-8")
            result = BrazosPropertyImport(CadRefreshStage(), None).run(
                PropertyImportRequest(
                    mode=PropertyImportMode.CAD_RECOVERY,
                    options=RefreshOptions(
                        tax_year=2026, skip_download=True, skip_extract=True, dry_run=True
                    ),
                )
            )
            self.assertTrue(result.dry_run)

    def test_uncertain_owner_blocks_and_is_prominent_in_admin(self):
        interrupted = ImportOperation.objects.create(
            county="harris", intent="property-only", requested_year=2026
        )
        CountyWriter.objects.create(county="harris", operation=interrupted, backend_pid=None)
        with (
            tempfile.TemporaryDirectory() as root,
            self.settings(
                HCAD_DOWNLOAD_DIR=str(Path(root) / "downloads"),
                HCAD_EXTRACT_DIR=str(Path(root) / "extracted"),
                HCAD_LOG_DIR=str(Path(root) / "logs"),
            ),
        ):
            with self.assertRaises(WriterConflict) as rejected:
                run_harris_import(
                    HarrisImportRequest(
                        plan=HarrisImportPlan.from_legacy_scope("property-only"),
                        acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                        extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                    )
                )
        self.assertEqual(rejected.exception.operation_id, interrupted.pk)
        self.assertTrue(rejected.exception.uncertain)
        user = get_user_model().objects.create_superuser("auditor", password="test")
        self.client.force_login(user)
        response = self.client.get(reverse("admin:data_importoperation_changelist"))
        self.assertContains(response, "Recovery required")
        self.assertContains(response, str(interrupted.pk))

    def test_same_county_rejects_with_owner_while_other_county_runs(self):
        held, release = Event(), Event()
        results, failures = [], []

        class HoldingReporter:
            def report(self, event):
                held.set()
                if not release.wait(15):
                    raise TimeoutError("Test did not release writer")

        with (
            tempfile.TemporaryDirectory() as root,
            self.settings(
                HCAD_DOWNLOAD_DIR=str(Path(root) / "harris" / "downloads"),
                HCAD_EXTRACT_DIR=str(Path(root) / "harris" / "extracted"),
                HCAD_LOG_DIR=str(Path(root) / "logs"),
                BCAD_DOWNLOAD_DIR=str(Path(root) / "brazos" / "downloads"),
                BCAD_EXTRACT_DIR=str(Path(root) / "brazos" / "extracted"),
            ),
        ):
            source = Path(root) / "harris" / "extracted" / "Real_acct_owner" / "real_acct.txt"
            source.parent.mkdir(parents=True)
            source.write_text("acct\tstate_class\nP100\tA1\n", encoding="latin-1")
            _stage_complete_pacs_export(Path(root) / "brazos", 2026)
            request = HarrisImportRequest(
                plan=HarrisImportPlan.from_legacy_scope("property-only"),
                data_year=2026,
                acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                load=HarrisApply(
                    refresh_readiness=False,
                    validate_completeness=False,
                    extracted_source_retention=ExtractedSourceRetention.RETAIN,
                ),
            )

            def run():
                try:
                    results.append(run_harris_import(request, reporter=HoldingReporter()))
                except Exception as exc:
                    failures.append(exc)
                finally:
                    connections.close_all()

            worker = Thread(target=run)
            worker.start()
            try:
                self.assertTrue(held.wait(15), "First import never reached held state")
                with self.assertRaises(WriterConflict) as rejected:
                    run_harris_import(request)
                owner = rejected.exception.operation_id
                user = get_user_model().objects.create_superuser("auditor", password="test")
                self.client.force_login(user)
                response = self.client.get(
                    reverse("admin:data_importoperation_change", args=[owner])
                )
                self.assertContains(response, "Active writer")
                other = BrazosPropertyImport(CadRefreshStage(), None).run(
                    PropertyImportRequest(
                        mode=PropertyImportMode.CAD_RECOVERY,
                        options=RefreshOptions(
                            tax_year=2026, skip_download=True, skip_extract=True, dry_run=True
                        ),
                    )
                )
                self.assertTrue(other.dry_run)
            finally:
                release.set()
                worker.join(20)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(results[0].operation_id, owner)
            self.assertTrue(run_harris_import(request).success)
