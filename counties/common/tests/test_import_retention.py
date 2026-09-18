"""Observe retirement through imports, cleanup commands and admin requests."""

import io
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase
from django.urls import reverse

from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
    RefreshOptions,
)
from counties.brazos.tests.test_property_coverage import write_pacs
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation


class SourceRetentionTests(TransactionTestCase):
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
        self.user = get_user_model().objects.create_superuser("retention", password="test")
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

    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.all():
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def publish(self):
        return BrazosPropertyImport(CadRefreshStage(), None).run(self.request)

    def test_harris_supersession_protects_current_sources_and_repeat_is_safe(self):
        from counties.harris.etl_pipeline import run_harris_import
        from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
        from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings
        from counties.harris.etl_pipeline.tests.test_import_publication import harris_request
        from counties.harris.models import BuildingDetail, PropertyRecord

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
            partial = run_harris_import(
                replace(request, plan=HarrisImportPlan.from_legacy_scope("building-only"))
            )
            old = ImportCandidate.objects.get(pk=first.candidate_id)
            inherited = ImportCandidate.objects.get(pk=partial.candidate_id)
            self.assertTrue(any(s.get("reference") == "inherited" for s in inherited.sources))
            with patch(
                "counties.common.import_retention.timezone.now",
                return_value=old.superseded_at + timedelta(days=90),
            ):
                call_command(
                    "cleanup_import_sources",
                    county="harris",
                    reason="Protected inherited",
                    stdout=io.StringIO(),
                )
            self.assertTrue(all(Path(s["path"]).is_file() for s in old.sources))
            source = Path(root) / "extracted/Real_acct_owner/real_acct.txt"
            source.write_text(source.read_text().replace("250000", "260000"))
            second = run_harris_import(request)
            old = ImportCandidate.objects.get(pk=first.candidate_id)
            current = ImportCandidate.objects.get(pk=second.candidate_id)
            inherited.refresh_from_db()
            self.assertEqual(old.state, "superseded")
            with patch(
                "counties.common.import_retention.timezone.now",
                return_value=inherited.superseded_at + timedelta(days=90),
            ):
                call_command(
                    "cleanup_import_sources",
                    county="harris",
                    reason="Expired",
                    stdout=io.StringIO(),
                )
            self.assertFalse(any(Path(s["path"]).is_file() for s in old.sources))
            self.assertTrue(all(Path(s["path"]).is_file() for s in current.sources))
            repeated = run_harris_import(replace(request, candidate_id=old.pk))
            self.assertTrue(repeated.already_applied)
            self.assertEqual(BuildingDetail.objects.get().heat_area, 1900)
            current.refresh_from_db()
            self.assertEqual(current.state, "published")

    def test_brazos_gis_recovery_retains_cad_until_complete_replacement(self):
        from counties.brazos.gis_refresh import GisRefreshStage
        from counties.brazos.tests.test_property_coverage import write_gis

        first = self.publish()
        write_gis(self.root, ["000000010013"])
        recovery = replace(self.request, mode=PropertyImportMode.GIS_RECOVERY)
        result = BrazosPropertyImport(CadRefreshStage(), GisRefreshStage()).run(recovery)
        candidate = ImportCandidate.objects.get(pk=result.candidate_id)
        url = reverse("admin:data_importcandidate_review", args=[candidate.pk])
        binding = self.client.get(url).context["form"]["binding"].value()
        self.assertEqual(
            self.client.post(
                url, {"decision": "approved", "reason": "Verified recovery", "binding": binding}
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                reverse("admin:data_importcandidate_apply", args=[candidate.pk]),
                {"reason": "Publish recovery"},
            ).status_code,
            302,
        )
        original = ImportCandidate.objects.get(pk=first.candidate_id)
        with patch(
            "counties.common.import_retention.timezone.now",
            return_value=original.superseded_at + timedelta(days=90),
        ):
            self.cleanup()
        self.assertTrue(all(Path(s["path"]).is_file() for s in original.sources))
        source = self.root / "extracted/2026/APPRAISAL_INFO.TXT"
        source.write_text(source.read_text().replace("Candidate owner", "Fresh owner    "))
        fresh = self.publish()
        candidate.refresh_from_db()
        with patch(
            "counties.common.import_retention.timezone.now",
            return_value=candidate.superseded_at + timedelta(days=90),
        ):
            self.cleanup()
        self.assertFalse(any(Path(s["path"]).is_file() for s in original.sources))
        current = ImportCandidate.objects.get(pk=fresh.candidate_id)
        self.assertTrue(all(Path(s["path"]).is_file() for s in current.sources))

    def retired(self):
        first = self.publish()
        source = self.root / "extracted" / "2026" / "APPRAISAL_INFO.TXT"
        source.write_text(source.read_text().replace("Candidate owner", "Changed owner  "))
        second = self.publish()
        old = ImportCandidate.objects.get(pk=first.candidate_id)
        current = ImportCandidate.objects.get(pk=second.candidate_id)
        self.assertEqual(old.state, "superseded")
        self.assertIsNotNone(old.published_at)
        self.assertIsNotNone(old.superseded_at)
        return old, current

    def cleanup(self):
        call_command(
            "cleanup_import_sources",
            county="brazos",
            actor="retention",
            reason="Remove expired sources",
            stdout=io.StringIO(),
        )

    def test_ninety_day_boundary_preserves_current_and_audit(self):
        old, current = self.retired()
        deadline = old.superseded_at + timedelta(days=90)
        with patch(
            "counties.common.import_retention.timezone.now",
            return_value=deadline - timedelta(microseconds=1),
        ):
            self.cleanup()
        self.assertTrue(all(Path(s["path"]).is_file() for s in old.sources))
        with patch("counties.common.import_retention.timezone.now", return_value=deadline):
            self.cleanup()
        self.assertFalse(any(Path(s["path"]).is_file() for s in old.sources))
        self.assertTrue(all(Path(s["path"]).is_file() for s in current.sources))
        old.refresh_from_db()
        self.assertTrue(old.sources)
        self.assertTrue(old.operation.audit_entries.filter(kind="supersession").exists())
        self.assertTrue(old.operation.audit_entries.filter(kind="source_cleanup").exists())
        self.assertContains(
            self.client.get(reverse("admin:data_importcandidate_change", args=[old.pk])),
            "Unavailable",
        )
        response = self.client.get(
            reverse("admin:data_importoperation_change", args=[old.operation_id])
        )
        self.assertContains(response, "source_cleanup")
        self.assertContains(response, old.sources[0]["sha256"])
        repeated = BrazosPropertyImport(CadRefreshStage(), None).run(
            replace(self.request, candidate_id=old.pk)
        )
        self.assertTrue(repeated.already_applied)
        self.assertEqual(PropertyAccount.objects.get().owner_name, "Changed owner")
        self.assertEqual(ImportAuditEntry.objects.filter(kind="publication").count(), 2)

    def test_shared_blocked_and_unmanaged_references_are_protected(self):
        old, current = self.retired()
        shared = old.sources[0]
        current.sources.append(shared)
        current.state = "blocked"
        current.save(update_fields=["sources", "state"])
        outside = self.root / "operator-original.txt"
        outside.write_text("original")
        old.sources.append({"path": str(outside), "sha256": "recorded"})
        old.save(update_fields=["sources"])
        with patch(
            "counties.common.import_retention.timezone.now",
            return_value=old.superseded_at + timedelta(days=90),
        ):
            self.cleanup()
        self.assertTrue(Path(shared["path"]).is_file())
        self.assertTrue(outside.is_file())
        self.assertTrue(all(Path(s["path"]).is_file() for s in current.sources))
        results = old.operation.audit_entries.get(kind="source_cleanup").evidence["sources"]
        self.assertEqual(results[0]["result"], "protected")
        self.assertEqual(results[-1]["result"], "unmanaged")

    def test_cleanup_failure_warns_and_cannot_repeat_publication(self):
        old, current = self.retired()
        with (
            patch(
                "counties.common.import_retention.timezone.now",
                return_value=old.superseded_at + timedelta(days=90),
            ),
            patch("pathlib.Path.unlink", side_effect=OSError("disk denied")),
        ):
            self.cleanup()
        operation = ImportOperation.objects.get(intent="source_cleanup")
        self.assertEqual(operation.status, "completed_with_warnings")
        self.assertIn("disk denied", str(operation.warnings))
        self.assertTrue(all(Path(s["path"]).is_file() for s in old.sources))
        self.assertEqual(
            old.operation.audit_entries.get(kind="source_cleanup").result, "completed_with_warnings"
        )
        repeated = BrazosPropertyImport(CadRefreshStage(), None).run(
            replace(self.request, candidate_id=current.pk)
        )
        self.assertTrue(repeated.already_applied)
        self.assertEqual(ImportAuditEntry.objects.filter(kind="publication").count(), 2)

    def test_rejection_clock_admin_permission_and_reason(self):
        result = BrazosPropertyImport(CadRefreshStage(), None).run(
            replace(self.request, prepare_only=True)
        )
        candidate = ImportCandidate.objects.get(pk=result.candidate_id)
        review = reverse("admin:data_importcandidate_review", args=[candidate.pk])
        binding = self.client.get(review).context["form"]["binding"].value()
        self.assertEqual(
            self.client.post(
                review, {"decision": "rejected", "reason": "Unsuitable", "binding": binding}
            ).status_code,
            302,
        )
        candidate.refresh_from_db()
        self.assertIsNotNone(candidate.rejected_at)
        staff = get_user_model().objects.create_user("reader", password="test", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_importoperation"))
        self.client.force_login(staff)
        url = reverse("admin:data_importoperation_cleanup_sources", args=[candidate.operation_id])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.user)
        self.assertContains(self.client.post(url, {"reason": ""}), "This field is required")
        with patch(
            "counties.common.import_retention.timezone.now",
            return_value=candidate.rejected_at + timedelta(days=90),
        ):
            self.client.force_login(self.user)
            self.assertEqual(
                self.client.post(url, {"reason": "Expired rejected input"}).status_code, 302
            )
        self.assertFalse(any(Path(s["path"]).is_file() for s in candidate.sources))
        self.assertTrue(
            candidate.operation.audit_entries.filter(
                kind="coverage_review", result="rejected"
            ).exists()
        )
