"""Import evidence is observable through authorized Django admin requests."""

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.test import TestCase
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
from counties.brazos.tests.test_property_coverage import write_pacs
from counties.brazos.tests.test_property_import import _stage_complete_pacs_export
from counties.common.models import ImportOperation
from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisExtractionMode,
    HarrisImportRequest,
    HarrisPreview,
    run_harris_import,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan


class ImportAdminTests(TestCase):
    def test_observed_source_failure_is_available_in_a_new_admin_session(self):
        import geopandas as gpd
        from shapely.geometry import Point

        with (
            tempfile.TemporaryDirectory() as root,
            self.settings(
                BCAD_DOWNLOAD_DIR=str(Path(root) / "downloads"),
                BCAD_EXTRACT_DIR=str(Path(root) / "extracted"),
            ),
        ):
            _stage_complete_pacs_export(Path(root), 2026)
            shape = Path(root) / "extracted" / "gis" / "2026" / "parcels.shp"
            shape.parent.mkdir(parents=True)
            gpd.GeoDataFrame(
                {"PROP_ID": ["invalid"]}, geometry=[Point(3556000, 10120000)], crs="EPSG:2277"
            ).to_file(shape)
            with self.assertRaisesMessage(CommandError, "no usable parcel coordinates"):
                BrazosPropertyImport(CadRefreshStage(), GisRefreshStage()).run(
                    PropertyImportRequest(
                        mode=PropertyImportMode.ANNUAL,
                        options=RefreshOptions(
                            tax_year=2026,
                            skip_download=True,
                            skip_extract=True,
                            keep_extracted=True,
                        ),
                    )
                )
            operation = ImportOperation.objects.get(county="brazos")
        user = get_user_model().objects.create_superuser("auditor", password="test")
        self.client.force_login(user)
        response = self.client.get(
            reverse("admin:data_importoperation_change", args=[operation.pk])
        )
        self.assertContains(response, "no usable parcel coordinates")
        self.assertContains(response, "failed")

    def test_brazos_publication_records_its_observed_snapshot(self):
        PropertyAccount.objects.create(tax_year=2026, prop_id="000000010013", owner_name="Old")
        BrazosPropertySnapshot.objects.create(
            tax_year=2026, cad_source_year=2026, outcome="partial"
        )
        with (
            tempfile.TemporaryDirectory() as root,
            self.settings(
                BCAD_DOWNLOAD_DIR=str(Path(root) / "downloads"),
                BCAD_EXTRACT_DIR=str(Path(root) / "extracted"),
            ),
        ):
            write_pacs(Path(root), ["000000010013"])
            result = BrazosPropertyImport(CadRefreshStage(), None).run(
                PropertyImportRequest(
                    mode=PropertyImportMode.CAD_RECOVERY,
                    options=RefreshOptions(
                        tax_year=2026, skip_download=True, skip_extract=True, keep_extracted=True
                    ),
                )
            )
        user = get_user_model().objects.create_superuser("auditor", password="test")
        self.client.force_login(user)
        response = self.client.get(
            reverse("admin:data_importoperation_change", args=[result.operation_id])
        )
        self.assertContains(response, "snapshot_id")
        self.assertContains(response, str(result.snapshot_id))
        self.assertContains(response, "Observed atomic publication")

    def test_brazos_failure_is_durable_and_does_not_claim_publication(self):
        with (
            tempfile.TemporaryDirectory() as root,
            self.settings(
                BCAD_DOWNLOAD_DIR=str(Path(root) / "downloads"),
                BCAD_EXTRACT_DIR=str(Path(root) / "extracted"),
            ),
        ):
            source = Path(root) / "extracted" / "2026" / "APPRAISAL_INFO.TXT"
            source.parent.mkdir(parents=True)
            source.write_text("incomplete", encoding="utf-8")
            with self.assertRaises(CommandError):
                BrazosPropertyImport(CadRefreshStage(), None).run(
                    PropertyImportRequest(
                        mode=PropertyImportMode.CAD_RECOVERY,
                        options=RefreshOptions(
                            tax_year=2026, skip_download=True, skip_extract=True
                        ),
                        actor="reviewing operator",
                    )
                )
        user = get_user_model().objects.create_superuser("auditor", password="test")
        self.client.force_login(user)
        response = self.client.get(reverse("admin:data_importoperation_changelist"))
        self.assertContains(response, "Brazos")
        self.assertContains(response, "failed")
        operation = response.context["cl"].result_list[0]
        detail = self.client.get(reverse("admin:data_importoperation_change", args=[operation.pk]))
        self.assertContains(detail, "reviewing operator")
        self.assertContains(detail, "missing required PACS")
        self.assertContains(detail, "Not recorded")

    def test_audit_requires_permission_and_cannot_be_edited(self):
        url = reverse("admin:data_importoperation_changelist")
        self.assertEqual(self.client.get(url).status_code, 302)
        user = get_user_model().objects.create_user("staff", password="test", is_staff=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 403)
        user.is_superuser = True
        user.save()
        self.assertEqual(
            self.client.post(reverse("admin:data_importoperation_add"), {}).status_code, 403
        )

    def test_preview_evidence_survives_a_new_admin_session(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "extracted" / "Real_acct_owner" / "real_acct.txt"
            source.parent.mkdir(parents=True)
            source.write_text(
                "acct\tsite_addr_1\tstate_class\nP100\t100 MAIN ST\tA1\n",
                encoding="latin-1",
            )
            with self.settings(
                HCAD_DOWNLOAD_DIR=str(Path(root) / "downloads"),
                HCAD_EXTRACT_DIR=str(Path(root) / "extracted"),
                HCAD_LOG_DIR=str(Path(root) / "logs"),
            ):
                result = run_harris_import(
                    HarrisImportRequest(
                        plan=HarrisImportPlan.from_legacy_scope("property-only"),
                        data_year=2026,
                        acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                        extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                        load=HarrisPreview(),
                    )
                )
        user = get_user_model().objects.create_superuser("auditor", password="test")
        self.client.force_login(user)
        self.client.logout()
        self.client.force_login(user)
        response = self.client.get(reverse("admin:data_importoperation_changelist"))
        self.assertContains(response, str(result.operation_id))
        self.assertContains(response, "Harris")
        self.assertContains(response, "preview")
        detail = self.client.get(
            reverse("admin:data_importoperation_change", args=[result.operation_id])
        )
        self.assertContains(detail, "completed")
        self.assertContains(detail, "Not recorded")
        self.assertNotContains(detail, 'name="_save"')
