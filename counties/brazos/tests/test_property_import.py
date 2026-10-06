"""Contract tests for the county-owned Brazos property-import module."""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from counties.brazos.benchmark import write_complete_pacs_export as _stage_complete_pacs_export
from counties.brazos.cad_refresh import (
    IMPROVEMENT_DETAIL_ATTR_FILENAME,
    CadRefreshStage,
)
from counties.brazos.gis_refresh import GisRefreshStage
from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportOutcome,
    PropertyImportRequest,
    RefreshOptions,
)
from counties.common.models import ImportCandidate


class PropertyImportPublicationTests(TestCase):
    def _review_and_apply(self, candidate_id):
        candidate = ImportCandidate.objects.get(pk=candidate_id)
        self.assertEqual(candidate.state, "awaiting_review")
        user = get_user_model().objects.create_superuser("property-reviewer", password="test")
        self.client.force_login(user)
        url = reverse("admin:data_importcandidate_review", args=[candidate.pk])
        binding = self.client.get(url).context["form"]["binding"].value()
        self.assertEqual(
            self.client.post(
                url,
                {"decision": "approved", "reason": "Verified source fixtures", "binding": binding},
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                reverse("admin:data_importcandidate_apply", args=[candidate.pk]),
                {"reason": "Publish verified fixture"},
            ).status_code,
            302,
        )

    def test_qualified_cad_recovery_publishes_an_active_partial_snapshot(self):
        from counties.brazos.tests.test_property_coverage import write_pacs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_pacs(root, ["000000010013"], equity=True)
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
            ):
                result = BrazosPropertyImport(cad=CadRefreshStage(), gis=None).run(
                    PropertyImportRequest(
                        mode=PropertyImportMode.CAD_RECOVERY,
                        options=RefreshOptions(
                            tax_year=2026, skip_download=True, skip_extract=True
                        ),
                    )
                )
                self.assertFalse(BrazosPropertySnapshot.objects.exists())
                self._review_and_apply(result.candidate_id)

        self.assertEqual(result.outcome, PropertyImportOutcome.PARTIAL)
        self.assertEqual(result.tax_year, 2026)
        snapshot = BrazosPropertySnapshot.objects.get(is_active=True)
        self.assertEqual(snapshot.tax_year, 2026)
        self.assertEqual(snapshot.outcome, PropertyImportOutcome.PARTIAL)
        self.assertEqual(snapshot.cad_source_year, 2026)
        self.assertIsNone(snapshot.gis_source_year)
        self.assertTrue(PropertyAccount.objects.filter(tax_year=2026).exists())

    def test_incomplete_pacs_source_stops_before_replacing_the_active_snapshot(self):
        BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=PropertyImportOutcome.COMPLETED,
            cad_source_year=2025,
            gis_source_year=2025,
        )
        PropertyAccount.objects.create(prop_id="000000010013", tax_year=2025)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extract_dir = root / "extracted" / "2026"
            extract_dir.mkdir(parents=True)
            (extract_dir / "APPRAISAL_INFO.TXT").write_text("incomplete", encoding="utf-8")

            with (
                self.settings(
                    BCAD_DOWNLOAD_DIR=str(root / "downloads"),
                    BCAD_EXTRACT_DIR=str(root / "extracted"),
                ),
                self.assertRaisesRegex(CommandError, "missing required PACS"),
            ):
                BrazosPropertyImport(cad=CadRefreshStage(), gis=None).run(
                    PropertyImportRequest(
                        mode=PropertyImportMode.CAD_RECOVERY,
                        options=RefreshOptions(
                            tax_year=2026, skip_download=True, skip_extract=True
                        ),
                    )
                )

        active = BrazosPropertySnapshot.objects.get(is_active=True)
        self.assertEqual(active.tax_year, 2025)
        self.assertFalse(PropertyAccount.objects.filter(tax_year=2026).exists())

    def test_empty_required_detail_file_stops_before_replacing_the_active_snapshot(self):
        BrazosPropertySnapshot.objects.create(
            tax_year=2025,
            outcome=PropertyImportOutcome.COMPLETED,
            cad_source_year=2025,
            gis_source_year=2025,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stage_complete_pacs_export(root, 2026)
            (root / "extracted" / "2026" / IMPROVEMENT_DETAIL_ATTR_FILENAME).write_text(
                "", encoding="utf-8"
            )

            with (
                self.settings(
                    BCAD_DOWNLOAD_DIR=str(root / "downloads"),
                    BCAD_EXTRACT_DIR=str(root / "extracted"),
                ),
                self.assertRaisesRegex(CommandError, IMPROVEMENT_DETAIL_ATTR_FILENAME),
            ):
                BrazosPropertyImport(cad=CadRefreshStage(), gis=None).run(
                    PropertyImportRequest(
                        mode=PropertyImportMode.CAD_RECOVERY,
                        options=RefreshOptions(
                            tax_year=2026, skip_download=True, skip_extract=True
                        ),
                    )
                )

        active = BrazosPropertySnapshot.objects.get(is_active=True)
        self.assertEqual(active.tax_year, 2025)

    def test_year_matched_gis_recovery_completes_the_active_partial_snapshot(self):
        BrazosPropertySnapshot.objects.create(
            tax_year=2026,
            outcome=PropertyImportOutcome.PARTIAL,
            cad_source_year=2026,
        )
        PropertyAccount.objects.create(
            prop_id="000000010013", tax_year=2026, owner_name="Partial CAD owner"
        )

        from counties.brazos.tests.test_property_coverage import write_gis

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_gis(root, ["000000010013"])
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"), BCAD_EXTRACT_DIR=str(root / "extracted")
            ):
                result = BrazosPropertyImport(cad=CadRefreshStage(), gis=GisRefreshStage()).run(
                    PropertyImportRequest(
                        mode=PropertyImportMode.GIS_RECOVERY,
                        options=RefreshOptions(
                            tax_year=2026, skip_download=True, skip_extract=True
                        ),
                    )
                )
                self.assertEqual(
                    BrazosPropertySnapshot.objects.get(is_active=True).outcome,
                    PropertyImportOutcome.PARTIAL,
                )
                self._review_and_apply(result.candidate_id)

        self.assertEqual(result.outcome, PropertyImportOutcome.COMPLETED)
        active = BrazosPropertySnapshot.objects.get(is_active=True)
        self.assertEqual(active.outcome, PropertyImportOutcome.COMPLETED)
        self.assertEqual(active.gis_source_year, 2026)
