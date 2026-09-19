"""Synthetic high-volume pipeline stress test for Brazos candidate staging."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from django.db import connection
from django.test import TransactionTestCase

from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
    RefreshOptions,
)
from counties.brazos.tests.test_property_import import _stage_complete_pacs_export
from counties.common.models import ImportCandidate


def _fast_write_pacs(root: Path, count: int = 5000, year: int = 2026) -> None:
    _stage_complete_pacs_export(root, year)
    identities = [str(i + 10000).zfill(12) for i in range(count)]
    extract_dir = root / "extracted" / str(year)
    for source in extract_dir.glob("*.TXT"):
        original = source.read_text().rstrip("\n")
        suffix = original[12:]
        if source.name == "APPRAISAL_INFO.TXT":
            prefix_to_owner = original[12:608]
            owner = "Candidate owner"
            after_owner = original[623:]
            lines = [f"{key}{prefix_to_owner}{owner}{after_owner}" for key in identities]
        else:
            lines = [f"{key}{suffix}" for key in identities]
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")


class BrazosPipelineStressTests(TransactionTestCase):
    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.filter(county="brazos"):
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def test_brazos_candidate_staging_under_synthetic_stress(self):
        record_count = 5000
        year = 2026

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _fast_write_pacs(root, count=record_count, year=year)

            start_time = time.monotonic()
            with self.settings(
                BCAD_DOWNLOAD_DIR=str(root / "downloads"),
                BCAD_EXTRACT_DIR=str(root / "extracted"),
            ):
                result = BrazosPropertyImport(CadRefreshStage(), None).run(
                    PropertyImportRequest(
                        mode=PropertyImportMode.CAD_RECOVERY,
                        options=RefreshOptions(
                            tax_year=year, skip_download=True, skip_extract=True
                        ),
                        prepare_only=True,
                    )
                )
            duration = time.monotonic() - start_time

            self.assertTrue(result.prepared)
            self.assertEqual(result.workflow_state, "awaiting_review")
            self.assertIsNotNone(result.candidate_id)
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            self.assertEqual(candidate.state, "awaiting_review")

            with connection.cursor() as cursor:
                cursor.execute(
                    f'SELECT count(*) FROM "{candidate.storage_schema}"."brazos_cad_propertyaccount"'
                )
                count = cursor.fetchone()[0]
                self.assertEqual(count, record_count)

            self.assertLess(
                duration, 90.0, f"5,000 Brazos records staged in {duration:.2f}s, expected <90s"
            )
