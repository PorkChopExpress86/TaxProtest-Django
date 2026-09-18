"""Coverage decisions cross authenticated admin requests and never publish."""

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase
from django.urls import reverse

from counties.brazos.annual_refresh import RefreshOptions
from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
)
from counties.brazos.tests.test_property_coverage import write_pacs
from counties.common.models import ImportAuditEntry, ImportCandidate


class ImportReviewTests(TransactionTestCase):
    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.all():
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def prepare(self, root, *, owners=True):
        write_pacs(root, ["000000010013"], owners=owners)
        with self.settings(
            BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
        ):
            result = BrazosPropertyImport(CadRefreshStage(), None).run(
                PropertyImportRequest(
                    mode=PropertyImportMode.CAD_RECOVERY,
                    options=RefreshOptions(tax_year=2026, skip_download=True, skip_extract=True),
                    prepare_only=True,
                )
            )
        return ImportCandidate.objects.get(pk=result.candidate_id)

    def reviewer(self):
        return get_user_model().objects.create_superuser("reviewer", password="test")

    def url(self, candidate):
        return reverse("admin:data_importcandidate_review", args=[candidate.pk])

    def binding(self, candidate):
        response = self.client.get(self.url(candidate))
        self.assertEqual(response.status_code, 200)
        return response.context["form"]["binding"].value()

    def test_permission_reason_approval_and_audit_are_separate_from_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            candidate = self.prepare(Path(temp))
            staff = get_user_model().objects.create_user("staff", password="test", is_staff=True)
            staff.user_permissions.add(
                Permission.objects.get(
                    codename="view_importcandidate", content_type__app_label="data"
                )
            )
            self.client.force_login(staff)
            self.assertEqual(self.client.get(self.url(candidate)).status_code, 403)
            user = self.reviewer()
            self.client.force_login(user)
            binding = self.binding(candidate)
            response = self.client.post(
                self.url(candidate), {"decision": "approved", "reason": "", "binding": binding}
            )
            self.assertContains(response, "This field is required")
            self.assertFalse(ImportAuditEntry.objects.filter(kind="coverage_review").exists())
            response = self.client.post(
                self.url(candidate),
                {
                    "decision": "approved",
                    "reason": "Reviewed first import and Partial limitations",
                    "binding": binding,
                },
            )
            self.assertEqual(response.status_code, 302)
            candidate.refresh_from_db()
            self.assertEqual(candidate.state, "approved")
            self.assertFalse(PropertyAccount.objects.exists())
            self.assertFalse(BrazosPropertySnapshot.objects.exists())
            audit = ImportAuditEntry.objects.get(kind="coverage_review")
            self.assertEqual(audit.actor, user.username)
            self.assertEqual(audit.evidence["binding"], binding)
            self.assertEqual(audit.result, "approved")
            detail = self.client.get(
                reverse("admin:data_importcandidate_change", args=[candidate.pk])
            )
            self.assertContains(detail, "Reviewed first import")
            self.assertContains(detail, "Approval does not publish")
            repeated = self.client.post(
                self.url(candidate), {"decision": "approved", "reason": "Again", "binding": binding}
            )
            self.assertContains(repeated, "already has a review decision")

    def test_source_candidate_and_baseline_changes_reject_stale_review(self):
        self.client.force_login(self.reviewer())
        for changed in ("source", "candidate", "baseline"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temp:
                candidate = self.prepare(Path(temp))
                binding = self.binding(candidate)
                if changed == "source":
                    Path(candidate.sources[0]["path"]).write_text("changed")
                elif changed == "candidate":
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f'UPDATE "{candidate.storage_schema}".brazos_cad_propertyaccount SET owner_name = %s',
                            ["Changed candidate"],
                        )
                else:
                    PropertyAccount.objects.create(
                        tax_year=2025, prop_id="BASE", owner_name="Changed baseline"
                    )
                response = self.client.post(
                    self.url(candidate),
                    {"decision": "approved", "reason": "Reviewed", "binding": binding},
                )
                self.assertEqual(response.status_code, 200)
                candidate.refresh_from_db()
                self.assertNotEqual(candidate.state, "approved")
                self.assertEqual(
                    ImportAuditEntry.objects.filter(
                        operation=candidate.operation, kind="coverage_review"
                    )
                    .latest("created_at")
                    .result,
                    "rejected",
                )

    def test_harris_first_import_can_be_reviewed_without_publishing(self):
        import geopandas as gpd
        from shapely.geometry import Point

        from counties.harris.etl_pipeline import (
            HarrisAcquisitionMode,
            HarrisExtractionMode,
            HarrisImportRequest,
            HarrisPrepare,
            run_harris_import,
        )
        from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
        from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings
        from counties.harris.models import PropertyRecord

        self.client.force_login(self.reviewer())
        with tempfile.TemporaryDirectory() as temp, self.settings(**_runtime_settings(temp)):
            extracted = Path(temp) / "extracted"
            accounts = extracted / "Real_acct_owner"
            buildings = extracted / "Real_building_land"
            gis = extracted / "Parcels"
            for directory in (accounts, buildings, gis):
                directory.mkdir(parents=True)
            (accounts / "real_acct.txt").write_text("acct\tstate_class\nP100\tA1\n")
            (buildings / "building_res.txt").write_text("acct\tbld_num\theat_ar\nP100\t1\t1800\n")
            (buildings / "fixtures.txt").write_text(
                "acct\tbld_num\ttype\tunits\nP100\t1\tRMB\t3\nP100\t1\tRMF\t2\n"
            )
            (buildings / "extra_features.txt").write_text("acct\tbld_num\tcd\nP100\t1\tGAR\n")
            gpd.GeoDataFrame(
                {"ACCT": ["P100"]}, geometry=[Point(3100000, 13800000)], crs="EPSG:2278"
            ).to_file(gis / "parcels.shp")
            result = run_harris_import(
                HarrisImportRequest(
                    plan=HarrisImportPlan.from_legacy_scope("full"),
                    data_year=2026,
                    acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                    extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                    load=HarrisPrepare(validate_completeness=False),
                )
            )
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            self.assertEqual(candidate.state, "awaiting_review")
            binding = self.binding(candidate)
            response = self.client.post(
                self.url(candidate),
                {
                    "decision": "approved",
                    "reason": "Reviewed Harris first import",
                    "binding": binding,
                },
            )
            self.assertEqual(response.status_code, 302)
            candidate.refresh_from_db()
            self.assertEqual(candidate.state, "approved")
            self.assertFalse(PropertyRecord.objects.exists())

    def test_source_invalid_candidate_can_be_rejected_but_not_approved(self):
        self.client.force_login(self.reviewer())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_pacs(root, ["000000010013"])
            source = root / "extracted" / "2026" / "APPRAISAL_INFO.TXT"
            source.write_text("invalid source layout\n")
            with (
                self.settings(
                    BCAD_DOWNLOAD_DIR=str(root / "downloads"),
                    BCAD_EXTRACT_DIR=str(root / "extracted"),
                ),
                self.assertRaises(CommandError),
            ):
                BrazosPropertyImport(CadRefreshStage(), None).run(
                    PropertyImportRequest(
                        mode=PropertyImportMode.CAD_RECOVERY,
                        options=RefreshOptions(
                            tax_year=2026, skip_download=True, skip_extract=True
                        ),
                        prepare_only=True,
                    )
                )
            candidate = ImportCandidate.objects.latest("created_at")
            binding = self.binding(candidate)
            response = self.client.post(
                self.url(candidate),
                {"decision": "approved", "reason": "Cannot override", "binding": binding},
            )
            self.assertContains(response, "Source integrity qualification failed")
            response = self.client.post(
                self.url(candidate),
                {"decision": "rejected", "reason": "Invalid source layout", "binding": binding},
            )
            self.assertEqual(response.status_code, 302)
            candidate.refresh_from_db()
            self.assertEqual(candidate.state, "rejected")
            self.assertFalse(PropertyAccount.objects.exists())

    def test_zero_eligible_cannot_be_approved_and_rejection_is_explicit(self):
        self.client.force_login(self.reviewer())
        with tempfile.TemporaryDirectory() as temp:
            candidate = self.prepare(Path(temp), owners=False)
            response = self.client.get(self.url(candidate))
            self.assertContains(response, "zero eligible")
            binding = response.context["form"]["binding"].value()
            response = self.client.post(
                self.url(candidate),
                {"decision": "approved", "reason": "Cannot override", "binding": binding},
            )
            self.assertContains(response, "zero eligible")
            candidate.refresh_from_db()
            self.assertEqual(candidate.state, "blocked")
            self.assertEqual(
                self.client.post(
                    self.url(candidate),
                    {
                        "decision": "rejected",
                        "reason": "Zero eligible population",
                        "binding": binding,
                    },
                ).status_code,
                302,
            )
            candidate.refresh_from_db()
            self.assertEqual(candidate.state, "rejected")
        with tempfile.TemporaryDirectory() as temp:
            candidate = self.prepare(Path(temp))
            binding = self.binding(candidate)
            self.assertEqual(
                self.client.post(
                    self.url(candidate),
                    {
                        "decision": "rejected",
                        "reason": "Source choice not accepted",
                        "binding": binding,
                    },
                ).status_code,
                302,
            )
            candidate.refresh_from_db()
            self.assertEqual(candidate.state, "rejected")
