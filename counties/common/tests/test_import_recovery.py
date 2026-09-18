"""Verified writer recovery is an authorized, audited admin operation."""

import tempfile
import zipfile
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.urls import reverse

from counties.common.import_writers import FencedWriter, WriterConflict
from counties.common.models import CountyWriter, ImportOperation
from counties.harris.etl_pipeline import (
    ExtractedSourceRetention,
    HarrisAcquisitionMode,
    HarrisApply,
    HarrisExtractionMode,
    HarrisImportPhase,
    HarrisImportRequest,
    HarrisPreview,
    run_harris_import,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.tests.test_harris_import import (
    _runtime_settings,
    _write_property_source,
)
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG


class ImportRecoveryTests(TransactionTestCase):
    def test_fresh_preview_sources_can_be_reused_by_next_preview(self):
        payload = BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr(
                "real_acct.txt",
                "acct\tstate_class\tsitus_num\tsitus_street\nP100\tA1\t123\tMain\n",
            )
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.iter_content.return_value = [payload.getvalue()]
        with (
            tempfile.TemporaryDirectory() as root,
            self.settings(**_runtime_settings(root)),
            patch("requests.Session.get", return_value=response),
            patch("requests.Session.head", return_value=MagicMock(status_code=200)),
        ):
            request = HarrisImportRequest(
                plan=HarrisImportPlan.from_legacy_scope("property-only"),
                load=HarrisPreview(),
            )
            fresh = run_harris_import(request)
            reused = run_harris_import(
                replace(
                    request,
                    acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                    extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                )
            )
            self.assertTrue(fresh.success)
            self.assertTrue(reused.success)
            self.assertEqual(reused.stages[HarrisImportPhase.LOAD].metrics["records_loaded"], 1)
            user = get_user_model().objects.create_superuser("viewer", password="test")
            self.client.force_login(user)
            detail = self.client.get(
                reverse("admin:data_importoperation_change", args=[reused.operation_id])
            )
            self.assertContains(detail, "reused_from")
            self.assertContains(detail, str(fresh.operation_id))

    def test_recovery_contention_is_blocked_and_audited(self):
        operation = self.interrupted_writer()
        held, release = Event(), Event()
        failures = []

        def hold_reservation():
            try:
                with transaction.atomic():
                    CountyWriter.objects.select_for_update().get(county="harris")
                    held.set()
                    if not release.wait(15):
                        raise TimeoutError("Test did not release reservation")
            except Exception as exc:
                failures.append(exc)
            finally:
                connections.close_all()

        worker = Thread(target=hold_reservation)
        worker.start()
        try:
            self.assertTrue(held.wait(15))
            user = get_user_model().objects.create_superuser("recovery", password="test")
            self.client.force_login(user)
            response = self.client.post(
                reverse("admin:data_importoperation_recover_writer", args=[operation.pk]),
                {"reason": "Investigated uncertain reservation"},
            )
            self.assertContains(response, "writer reservation is busy or unavailable")
        finally:
            release.set()
            worker.join(20)
        self.assertEqual(failures, [])
        detail = self.client.get(reverse("admin:data_importoperation_change", args=[operation.pk]))
        self.assertContains(detail, "Recovery required")
        self.assertContains(detail, "Investigated uncertain reservation")
        self.assertContains(detail, "rejected")

    def test_resumed_download_cannot_overwrite_new_writer_inputs(self):
        interrupted, release = Event(), Event()
        old_database = []
        failures = []
        payload = BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("real_acct.txt", "acct\tstate_class\nOLD\tA1\n")

        def chunks(**kwargs):
            old_database[0].close()
            interrupted.set()
            if not release.wait(15):
                raise TimeoutError("Test did not release old download")
            yield payload.getvalue()

        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.iter_content.side_effect = chunks
        probe = MagicMock(status_code=200)

        with (
            tempfile.TemporaryDirectory() as root,
            self.settings(**_runtime_settings(root)),
            patch("requests.Session.get", return_value=response),
            patch("requests.Session.head", return_value=probe),
        ):
            _write_property_source(root)
            request = HarrisImportRequest(
                plan=HarrisImportPlan.from_legacy_scope("property-only"),
                load=HarrisApply(refresh_readiness=False, validate_completeness=False),
            )

            def run():
                try:
                    connections["default"].ensure_connection()
                    old_database.append(connections["default"].connection)
                    run_harris_import(request)
                except Exception as exc:
                    failures.append(exc)
                finally:
                    connections.close_all()

            worker = Thread(target=run)
            worker.start()
            try:
                self.assertTrue(interrupted.wait(15))
                with self.assertRaises(WriterConflict) as rejected:
                    run_harris_import(
                        replace(request, acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED)
                    )
                user = get_user_model().objects.create_superuser("recovery", password="test")
                self.client.force_login(user)
                recovery = self.client.post(
                    reverse(
                        "admin:data_importoperation_recover_writer",
                        args=[rejected.exception.operation_id],
                    ),
                    {"reason": "Disconnected acquisition investigated"},
                    follow=True,
                )
                self.assertContains(recovery, "Writer reservation safely released")
                catalog_source = request.plan.select_sources(
                    DEFAULT_HCAD_SOURCE_CATALOG.required_sources()
                )[0]
                current_archive = Path(root) / "downloads" / catalog_source.filename
                current_archive.write_bytes(b"new writer retained source")
                new_source = _write_property_source(root, account="NEW")
                new_contents = new_source.read_bytes()
                current = run_harris_import(
                    replace(
                        request,
                        acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                        extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                        load=HarrisPreview(),
                    )
                )
                self.assertTrue(current.success)
            finally:
                release.set()
                worker.join(20)
            self.assertEqual(current_archive.read_bytes(), b"new writer retained source")
            self.assertEqual(new_source.read_bytes(), new_contents)
            self.assertEqual(len(failures), 1)

    def test_resumed_old_execution_cannot_mutate_replacement_sources(self):
        interrupted, release = Event(), Event()
        failures = []

        class DisconnectedReporter:
            def report(self, event):
                connections["default"].close()
                interrupted.set()
                if not release.wait(15):
                    raise TimeoutError("Test did not release interrupted execution")

        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            source = _write_property_source(root)
            request = HarrisImportRequest(
                plan=HarrisImportPlan.from_legacy_scope("property-only"),
                acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                load=HarrisApply(refresh_readiness=False, validate_completeness=False),
            )

            def run():
                try:
                    run_harris_import(request, reporter=DisconnectedReporter())
                except Exception as exc:
                    failures.append(exc)
                finally:
                    connections.close_all()

            worker = Thread(target=run)
            worker.start()
            try:
                self.assertTrue(interrupted.wait(15))
                with self.assertRaises(WriterConflict) as rejected:
                    run_harris_import(request)
                owner = rejected.exception.operation_id
                user = get_user_model().objects.create_superuser("recovery", password="test")
                self.client.force_login(user)
                response = self.client.post(
                    reverse("admin:data_importoperation_recover_writer", args=[owner]),
                    {"reason": "Investigated disconnected writer"},
                    follow=True,
                )
                self.assertContains(response, "Writer reservation safely released")
                _write_property_source(root, account="P200")
                replacement = run_harris_import(
                    replace(
                        request,
                        load=HarrisApply(
                            refresh_readiness=False,
                            validate_completeness=False,
                            extracted_source_retention=ExtractedSourceRetention.RETAIN,
                        ),
                    )
                )
                self.assertTrue(replacement.success)
            finally:
                release.set()
                worker.join(20)
            self.assertTrue(source.exists(), "Resumed old cleanup removed replacement sources")
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], FencedWriter)
            detail = self.client.get(reverse("admin:data_importoperation_change", args=[owner]))
            self.assertContains(detail, "Investigated disconnected writer")

    def test_task_timeout_cannot_release_a_present_database_session(self):
        operation = ImportOperation.objects.create(county="harris", intent="property-only")
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pid = cursor.fetchone()[0]
        CountyWriter.objects.create(county="harris", operation=operation, backend_pid=pid)
        user = get_user_model().objects.create_superuser("recovery", password="test")
        self.client.force_login(user)
        response = self.client.post(
            reverse("admin:data_importoperation_recover_writer", args=[operation.pk]),
            {"reason": "Task result missing; timeout elapsed"},
        )
        self.assertContains(response, "original database session is still present")
        detail = self.client.get(reverse("admin:data_importoperation_change", args=[operation.pk]))
        self.assertContains(detail, "Recovery required")
        self.assertContains(detail, "rejected")

    def interrupted_writer(self):
        operation = ImportOperation.objects.create(county="harris", intent="property-only")
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            pid = cursor.fetchone()[0]
        connection.close()
        CountyWriter.objects.create(county="harris", operation=operation, backend_pid=pid)
        return operation

    def test_authorized_recovery_requires_reason_and_retains_its_audit(self):
        operation = self.interrupted_writer()
        user = get_user_model().objects.create_superuser("recovery operator", password="test")
        self.client.force_login(user)
        url = reverse("admin:data_importoperation_recover_writer", args=[operation.pk])
        self.assertContains(self.client.get(url), "Recovery required")
        self.assertContains(self.client.post(url, {}), "This field is required")
        response = self.client.post(
            url, {"reason": "Disconnected writer investigated"}, follow=True
        )
        self.assertContains(response, "Writer reservation safely released")
        self.client.logout()
        self.client.force_login(user)
        detail = self.client.get(reverse("admin:data_importoperation_change", args=[operation.pk]))
        self.assertContains(detail, "Disconnected writer investigated")
        self.assertContains(detail, "recovery operator")
        self.assertContains(detail, "database session ended")
        self.assertContains(self.client.post(url, {"reason": "Repeated request"}), "Stale recovery")

    def test_staff_view_permission_does_not_authorize_recovery(self):
        operation = self.interrupted_writer()
        user = get_user_model().objects.create_user("viewer", password="test", is_staff=True)
        user.user_permissions.add(Permission.objects.get(codename="view_importoperation"))
        self.client.force_login(user)
        url = reverse("admin:data_importoperation_recover_writer", args=[operation.pk])
        self.assertEqual(self.client.post(url, {"reason": "Timeout elapsed"}).status_code, 403)
        detail = self.client.get(reverse("admin:data_importoperation_change", args=[operation.pk]))
        self.assertContains(detail, "Recovery required")
