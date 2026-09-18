"""Historical replay uses new county candidates, admin review and publication."""

import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase
from django.urls import reverse

from counties.brazos.annual_refresh import RefreshOptions
from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.gis_refresh import GisRefreshStage
from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
)
from counties.brazos.tests.test_property_coverage import write_gis, write_pacs
from counties.common.models import CountyWriter, ImportAuditEntry, ImportCandidate, ImportOperation
from counties.harris.models import PropertyRecord


class ImportRecoveryTests(TransactionTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        context = self.settings(
            BCAD_DOWNLOAD_DIR=str(self.root / "downloads"),
            BCAD_EXTRACT_DIR=str(self.root / "extracted"),
        )
        context.enable()
        self.addCleanup(context.disable)
        self.user = get_user_model().objects.create_superuser("recovery", password="test")
        self.client.force_login(self.user)
        self.request = PropertyImportRequest(
            mode=PropertyImportMode.CAD_RECOVERY,
            options=RefreshOptions(tax_year=2026, skip_download=True, skip_extract=True),
        )
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2026, owner_name="Old")
        BrazosPropertySnapshot.objects.create(
            tax_year=2026, outcome="partial", cad_source_year=2026
        )
        write_pacs(self.root, ["000000010013"])
        first = BrazosPropertyImport(CadRefreshStage(), None).run(self.request)
        self.historical = ImportCandidate.objects.get(pk=first.candidate_id)
        source = self.root / "extracted/2026/APPRAISAL_INFO.TXT"
        source.write_text(source.read_text().replace("Candidate owner", "Current owner  "))
        current = BrazosPropertyImport(CadRefreshStage(), None).run(self.request)
        self.current = ImportCandidate.objects.get(pk=current.candidate_id)
        self.historical.refresh_from_db()
        self.url = reverse(
            "admin:data_importoperation_recover_dataset", args=[self.historical.operation_id]
        )

    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.all():
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def binding(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.context["form"]["binding"].value()

    def start(self, binding=None):
        return self.client.post(
            self.url,
            {"reason": "Restore verified prior source", "binding": binding or self.binding()},
        )

    def test_admin_requires_permission_reason_and_explains_year_and_availability(self):
        staff = get_user_model().objects.create_user("reader", password="test", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_importoperation"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.user)
        page = self.client.get(self.url)
        self.assertContains(page, "2026")
        self.assertContains(page, "Retained")
        self.assertContains(page, "new candidate")
        self.assertContains(
            self.client.post(self.url, {"reason": "", "binding": self.binding()}),
            "This field is required",
        )
        self.assertFalse(ImportOperation.objects.filter(origin="admin_recovery").exists())

    def test_recovery_prepares_fresh_baseline_then_restores_with_explicit_apply(self):
        other = PropertyRecord.objects.create(account_number="OTHER", owner_name="Harris owner")
        response = self.start()
        self.assertEqual(
            response.status_code, 302, response.context["form"].errors if response.context else None
        )
        operation = ImportOperation.objects.get(origin="admin_recovery")
        candidate = operation.candidate
        self.assertNotEqual(candidate.pk, self.historical.pk)
        self.assertEqual(
            candidate.baseline["snapshot_id"],
            self.current.operation.audit_entries.get(kind="publication").evidence["after"][
                "snapshot_id"
            ],
        )
        self.assertEqual(PropertyAccount.objects.get().owner_name, "Current owner")
        self.assertEqual(candidate.state, "prepared")
        from counties.brazos.adapter import adapter

        entered, resume = threading.Event(), threading.Event()
        responses, failures = [], []
        original = adapter.get_subject

        def pause_subject(key):
            subject = original(key)
            if threading.current_thread().name == "recovery-reader":
                entered.set()
                if not resume.wait(10):
                    raise TimeoutError("Recovery reader did not resume")
            return subject

        def read():
            close_old_connections()
            try:
                responses.append(
                    Client().get(reverse("brazos_similar_properties", args=["000000010013"]))
                )
            except Exception as exc:
                failures.append(exc)
            finally:
                close_old_connections()

        reader = threading.Thread(target=read, name="recovery-reader")
        with patch.object(adapter, "get_subject", side_effect=pause_subject):
            try:
                reader.start()
                self.assertTrue(entered.wait(5))
                applied = self.client.post(
                    reverse("admin:data_importcandidate_apply", args=[candidate.pk]),
                    {"reason": "Apply qualified recovery"},
                )
            finally:
                resume.set()
                reader.join(10)
        self.assertFalse(reader.is_alive())
        self.assertFalse(failures, failures)
        self.assertEqual(responses[0].context["subject"].owner_name, "Current owner")
        fresh = self.client.get(reverse("brazos_similar_properties", args=["000000010013"]))
        self.assertEqual(fresh.context["subject"].owner_name, "Candidate owner")
        self.assertEqual(applied.status_code, 302)
        self.assertEqual(PropertyAccount.objects.get().owner_name, "Candidate owner")
        active = BrazosPropertySnapshot.objects.get(is_active=True)
        self.assertNotEqual(active.pk, self.current.baseline["snapshot_id"])
        self.assertEqual(active.tax_year, 2026)
        self.assertEqual(BrazosPropertySnapshot.objects.filter(is_active=True).count(), 1)
        other.refresh_from_db()
        self.assertEqual(other.owner_name, "Harris owner")
        self.assertTrue(operation.audit_entries.filter(kind="dataset_recovery").exists())
        self.assertTrue(
            self.historical.operation.audit_entries.filter(kind="recovery_request").exists()
        )
        self.assertContains(
            self.client.get(reverse("admin:data_importoperation_change", args=[operation.pk])),
            str(self.historical.pk),
        )

    def test_missing_or_changed_source_is_visible_audited_and_preserves_current(self):
        binding = self.binding()
        Path(self.historical.sources[0]["path"]).unlink()
        response = self.start(binding)
        self.assertContains(response, "unavailable")
        operation = ImportOperation.objects.get(origin="admin_recovery")
        self.assertEqual(operation.status, "failed")
        self.assertTrue(operation.errors)
        self.assertEqual(PropertyAccount.objects.get().owner_name, "Current owner")
        self.assertTrue(
            self.historical.operation.audit_entries.filter(
                kind="recovery_request", result="failed"
            ).exists()
        )
        self.assertEqual(ImportAuditEntry.objects.filter(kind="publication").count(), 2)

    def test_stale_historical_evidence_is_rejected_without_publication(self):
        binding = self.binding()
        self.historical.sources[0]["source_year"] = 2025
        self.historical.save(update_fields=["sources"])
        response = self.start(binding)
        self.assertContains(response, "changed")
        self.assertEqual(ImportOperation.objects.get(origin="admin_recovery").status, "failed")
        self.assertEqual(PropertyAccount.objects.get().owner_name, "Current owner")

    def test_changed_source_digest_and_writer_conflict_are_audited(self):
        binding = self.binding()
        source = Path(self.historical.sources[0]["path"])
        source.write_text(source.read_text().replace("Candidate owner", "Tampered owner "))
        self.assertContains(self.start(binding), "changed")
        self.assertEqual(PropertyAccount.objects.get().owner_name, "Current owner")
        owner = ImportOperation.objects.create(county="brazos", intent="interrupted")
        CountyWriter.objects.update_or_create(county="brazos", defaults={"operation": owner})
        response = self.start(binding)
        self.assertContains(response, "Recovery required")
        self.assertEqual(
            ImportOperation.objects.filter(origin="admin_recovery", status="failed").count(), 2
        )
        self.assertEqual(
            self.historical.operation.audit_entries.filter(
                kind="recovery_request", result="failed"
            ).count(),
            2,
        )

    def test_restoring_lost_population_requires_fresh_coverage_review(self):
        source = self.root / "extracted/2026/APPRAISAL_INFO.TXT"
        raw = source.read_text().rstrip("\n")
        source.write_text(raw + "\n" + "000000010014" + raw[12:] + "\n")
        BrazosPropertyImport(CadRefreshStage(), None).run(self.request)
        self.assertEqual(PropertyAccount.objects.count(), 2)
        response = self.start()
        self.assertEqual(
            response.status_code, 302, response.context["form"].errors if response.context else None
        )
        candidate = ImportOperation.objects.get(origin="admin_recovery").candidate
        self.assertEqual(candidate.state, "awaiting_review")
        apply_url = reverse("admin:data_importcandidate_apply", args=[candidate.pk])
        self.assertContains(self.client.post(apply_url, {"reason": "Restore"}), "approval")
        self.assertEqual(PropertyAccount.objects.count(), 2)
        review_url = reverse("admin:data_importcandidate_review", args=[candidate.pk])
        binding = self.client.get(review_url).context["form"]["binding"].value()
        self.assertEqual(
            self.client.post(
                review_url, {"decision": "approved", "reason": "Reviewed loss", "binding": binding}
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(apply_url, {"reason": "Apply reviewed loss"}).status_code, 302
        )
        self.assertEqual(PropertyAccount.objects.count(), 1)

    def test_annual_recovery_revalidates_gis_and_failed_publication_preserves_current(self):
        self.root = self.root / "annual"
        context = self.settings(
            BCAD_DOWNLOAD_DIR=str(self.root / "downloads"),
            BCAD_EXTRACT_DIR=str(self.root / "extracted"),
        )
        context.enable()
        self.addCleanup(context.disable)
        ids = [f"{10013 + i:012d}" for i in range(4)]
        write_pacs(self.root, ids, equity=True)
        write_gis(self.root, ids)
        importer = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage())
        request = replace(self.request, mode=PropertyImportMode.ANNUAL)
        first = importer.run(request)
        historical = ImportCandidate.objects.get(pk=first.candidate_id)
        review_url = reverse("admin:data_importcandidate_review", args=[historical.pk])
        binding = self.client.get(review_url).context["form"]["binding"].value()
        self.assertEqual(
            self.client.post(
                review_url,
                {"decision": "approved", "reason": "Reviewed annual coverage", "binding": binding},
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                reverse("admin:data_importcandidate_apply", args=[historical.pk]),
                {"reason": "Publish annual inputs"},
            ).status_code,
            302,
        )
        source = self.root / "extracted/2026/APPRAISAL_INFO.TXT"
        source.write_text(source.read_text().replace("Candidate owner", "Current owner  "))
        current = importer.run(request)
        self.assertEqual(current.workflow_state, "published")
        historical.refresh_from_db()
        self.url = reverse(
            "admin:data_importoperation_recover_dataset", args=[historical.operation_id]
        )
        self.assertEqual(self.start().status_code, 302)
        candidate = ImportOperation.objects.get(origin="admin_recovery").candidate
        self.assertEqual(candidate.state, "prepared")
        self.assertEqual(PropertyAccount.objects.get(prop_id=ids[0]).owner_name, "Current owner")

        def fail(execute, sql, params, many, context):
            if sql.startswith('INSERT INTO "data_importauditentry"'):
                raise OSError("Restoration publication audit failed")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(fail), self.assertRaisesMessage(OSError, "Restoration"):
            importer.run(replace(request, candidate_id=candidate.pk))
        self.assertEqual(PropertyAccount.objects.get(prop_id=ids[0]).owner_name, "Current owner")
        self.assertEqual(BrazosPropertySnapshot.objects.get(is_active=True).pk, current.snapshot_id)
        candidate.refresh_from_db()
        self.assertNotEqual(candidate.state, "published")
        self.assertEqual(
            self.client.post(
                reverse("admin:data_importcandidate_apply", args=[candidate.pk]),
                {"reason": "Apply exact annual restoration"},
            ).status_code,
            302,
        )
        restored = BrazosPropertySnapshot.objects.get(is_active=True)
        self.assertEqual(restored.outcome, "completed")
        self.assertEqual((restored.cad_source_year, restored.gis_source_year), (2026, 2026))
        self.assertEqual(PropertyAccount.objects.get(prop_id=ids[0]).owner_name, "Candidate owner")
        self.assertEqual(BrazosPropertySnapshot.objects.filter(is_active=True).count(), 1)
        report = self.client.get(reverse("brazos_protest_analysis", args=[ids[0]]))
        self.assertEqual(report.status_code, 200)
        self.assertNotEqual(report.context["tax_impact"].completeness, "complete")

    def test_harris_recovery_replays_full_inputs_without_current_facts(self):
        from counties.harris.etl_pipeline import HarrisPropertyFile, run_harris_import
        from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings
        from counties.harris.etl_pipeline.tests.test_import_publication import harris_request
        from counties.harris.models import BuildingDetail

        prop = PropertyRecord.objects.create(
            account_number="P100",
            is_residential=True,
            is_data_ready=True,
            latitude=29.7,
            longitude=-95.4,
        )
        BuildingDetail.objects.create(
            property=prop,
            account_number="P100",
            building_number=1,
            bedrooms=3,
            bathrooms=2,
            heat_area=1800,
        )
        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            request = harris_request(root)
            first = run_harris_import(request)
            building = Path(root) / "extracted/Real_building_land/building_res.txt"
            building.write_text(building.read_text().replace("1800", "1900"))
            run_harris_import(request)
            self.assertEqual(BuildingDetail.objects.get().heat_area, 1900)
            historical = ImportCandidate.objects.get(pk=first.candidate_id)
            url = reverse(
                "admin:data_importoperation_recover_dataset", args=[historical.operation_id]
            )
            binding = self.client.get(url).context["form"]["binding"].value()
            self.assertEqual(
                self.client.post(
                    url, {"reason": "Replay prior Harris inputs", "binding": binding}
                ).status_code,
                302,
            )
            candidate = ImportOperation.objects.get(origin="admin_recovery").candidate
            self.assertEqual(BuildingDetail.objects.get().heat_area, 1900)
            self.assertEqual(
                self.client.post(
                    reverse("admin:data_importcandidate_apply", args=[candidate.pk]),
                    {"reason": "Apply verified Harris recovery"},
                ).status_code,
                302,
            )
            self.assertEqual(BuildingDetail.objects.get().heat_area, 1800)
            self.assertEqual(PropertyAccount.objects.get().owner_name, "Current owner")
            append_file = Path(root) / "append.txt"
            append_file.write_text("acct\tstate_class\ttot_appr_val\nP101\tA1\t100000\n")
            appended = run_harris_import(
                replace(request, property_file=HarrisPropertyFile(append_file, append=True))
            )
            self.assertTrue(appended.wrote_data)
            historical = ImportCandidate.objects.get(pk=appended.candidate_id)
            url = reverse(
                "admin:data_importoperation_recover_dataset", args=[historical.operation_id]
            )
            binding = self.client.get(url).context["form"]["binding"].value()
            refused = self.client.post(
                url, {"reason": "Replay append publication", "binding": binding}
            )
            self.assertContains(refused, "unavailable for append or limited publications")
            failed = ImportOperation.objects.filter(
                county="harris", origin="admin_recovery", status="failed"
            ).get()
            self.assertTrue(failed.audit_entries.filter(kind="dataset_recovery").exists())
            self.assertTrue(PropertyRecord.objects.filter(account_number="P101").exists())
