"""Synthetic high-volume pipeline stress test for Harris candidate staging."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from django.db import connection
from django.test import TransactionTestCase

from counties.common.models import ImportCandidate
from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisExtractionMode,
    HarrisImportRequest,
    HarrisImportStatus,
    HarrisPrepare,
    run_harris_import,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings


class HarrisPipelineStressTests(TransactionTestCase):
    def tearDown(self):
        with connection.cursor() as cursor:
            for candidate in ImportCandidate.objects.filter(county="harris"):
                cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
        super().tearDown()

    def _write_large_property_source(self, root: str, count: int = 5000) -> Path:
        source_dir = Path(root) / "extracted" / "Real_acct_owner"
        source_dir.mkdir(parents=True, exist_ok=True)
        source = source_dir / "real_acct.txt"
        lines = ["acct\tsite_addr_1\tsite_addr_3\tstate_class\ttot_appr_val"]
        for i in range(count):
            acct = f"P{i:07d}"
            lines.append(f"{acct}\t{i} MAIN ST\t77001\tA1\t{250000 + (i % 50000)}")
        source.write_text("\n".join(lines) + "\n", encoding="latin-1")
        return source

    def test_harris_candidate_staging_under_synthetic_stress(self):
        record_count = 5000
        request = HarrisImportRequest(
            plan=HarrisImportPlan.from_legacy_scope("property-only"),
            data_year=2026,
            acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
            extraction=HarrisExtractionMode.REUSE_EXTRACTED,
            load=HarrisPrepare(validate_completeness=False),
        )

        with tempfile.TemporaryDirectory() as root, self.settings(**_runtime_settings(root)):
            self._write_large_property_source(root, count=record_count)
            start_time = time.monotonic()
            result = run_harris_import(request)
            duration = time.monotonic() - start_time

            self.assertEqual(result.status, HarrisImportStatus.BLOCKED)
            self.assertIsNotNone(result.candidate_id)
            candidate = ImportCandidate.objects.get(pk=result.candidate_id)
            self.assertEqual(candidate.state, "blocked")

            with connection.cursor() as cursor:
                cursor.execute(
                    f'SELECT count(*) FROM "{candidate.storage_schema}"."data_propertyrecord"'
                )
                count = cursor.fetchone()[0]
                self.assertEqual(count, record_count)

            self.assertLess(
                duration, 10.0, f"5,000 records staged in {duration:.2f}s, expected <10s"
            )
