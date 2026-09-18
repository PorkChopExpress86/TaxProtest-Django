"""Verified writer recovery is an authorized, audited admin operation."""

import tempfile
from dataclasses import replace
from threading import Event, Thread

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection, connections
from django.test import TransactionTestCase
from django.urls import reverse

from counties.common.import_writers import FencedWriter, WriterConflict
from counties.common.models import CountyWriter, ImportOperation
from counties.harris.etl_pipeline import (
    ExtractedSourceRetention,
    HarrisAcquisitionMode,
    HarrisApply,
    HarrisExtractionMode,
    HarrisImportRequest,
    run_harris_import,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.tests.test_harris_import import (
    _runtime_settings,
    _write_property_source,
)


class ImportRecoveryTests(TransactionTestCase):
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
