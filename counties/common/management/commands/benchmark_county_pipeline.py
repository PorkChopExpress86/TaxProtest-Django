"""Benchmark county candidate staging throughput and memory consumption."""

from __future__ import annotations

import tempfile
import time
import tracemalloc
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from counties.common.models import ImportCandidate


def _benchmark_harris(rows: int) -> dict:
    from counties.harris.etl_pipeline import (
        HarrisAcquisitionMode,
        HarrisExtractionMode,
        HarrisImportRequest,
        HarrisPrepare,
        run_harris_import,
    )
    from counties.harris.etl_pipeline.import_plan import HarrisImportPlan

    request = HarrisImportRequest(
        plan=HarrisImportPlan.from_legacy_scope("property-only"),
        data_year=2026,
        acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
        extraction=HarrisExtractionMode.REUSE_EXTRACTED,
        load=HarrisPrepare(validate_completeness=False),
    )

    with tempfile.TemporaryDirectory() as root:
        base = Path(root)
        (base / "downloads").mkdir(parents=True, exist_ok=True)
        (base / "logs").mkdir(parents=True, exist_ok=True)
        source_dir = base / "extracted" / "Real_acct_owner"
        source_dir.mkdir(parents=True, exist_ok=True)
        source = source_dir / "real_acct.txt"
        lines = ["acct\tsite_addr_1\tsite_addr_3\tstate_class\ttot_appr_val"]
        for i in range(rows):
            acct = f"P{i:07d}"
            lines.append(f"{acct}\t{i} MAIN ST\t77001\tA1\t{250000 + (i % 50000)}")
        source.write_text("\n".join(lines) + "\n", encoding="latin-1")

        old_base = getattr(settings, "BASE_DIR", None)
        old_down = getattr(settings, "HCAD_DOWNLOAD_DIR", None)
        old_ext = getattr(settings, "HCAD_EXTRACT_DIR", None)
        old_log = getattr(settings, "HCAD_LOG_DIR", None)
        settings.BASE_DIR = str(base)
        settings.HCAD_DOWNLOAD_DIR = str(base / "downloads")
        settings.HCAD_EXTRACT_DIR = str(base / "extracted")
        settings.HCAD_LOG_DIR = str(base / "logs")

        tracemalloc.start()
        start_time = time.monotonic()
        try:
            result = run_harris_import(request)
        finally:
            duration = time.monotonic() - start_time
            _, peak_mem = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            if old_base is not None:
                settings.BASE_DIR = old_base
            if old_down is not None:
                settings.HCAD_DOWNLOAD_DIR = old_down
            if old_ext is not None:
                settings.HCAD_EXTRACT_DIR = old_ext
            if old_log is not None:
                settings.HCAD_LOG_DIR = old_log

        if result.candidate_id:
            try:
                candidate = ImportCandidate.objects.get(pk=result.candidate_id)
                with connection.cursor() as cursor:
                    cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
                candidate.delete()
            except ImportCandidate.DoesNotExist:
                pass

        return {
            "county": "harris",
            "rows": rows,
            "duration": duration,
            "throughput": rows / duration if duration > 0 else 0.0,
            "peak_memory_mb": peak_mem / (1024 * 1024),
            "status": result.status.name,
        }


def _benchmark_brazos(rows: int) -> dict:
    from counties.brazos.cad_refresh import CadRefreshStage
    from counties.brazos.property_import import (
        BrazosPropertyImport,
        PropertyImportMode,
        PropertyImportRequest,
        RefreshOptions,
    )
    from counties.brazos.tests.test_pipeline_stress import _fast_write_pacs

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "downloads").mkdir(parents=True, exist_ok=True)
        (root / "logs").mkdir(parents=True, exist_ok=True)
        _fast_write_pacs(root, count=rows, year=2026)

        old_down = getattr(settings, "BCAD_DOWNLOAD_DIR", None)
        old_ext = getattr(settings, "BCAD_EXTRACT_DIR", None)
        old_log = getattr(settings, "BCAD_LOG_DIR", None)
        settings.BCAD_DOWNLOAD_DIR = str(root / "downloads")
        settings.BCAD_EXTRACT_DIR = str(root / "extracted")
        settings.BCAD_LOG_DIR = str(root / "logs")

        tracemalloc.start()
        start_time = time.monotonic()
        try:
            result = BrazosPropertyImport(CadRefreshStage(), None).run(
                PropertyImportRequest(
                    mode=PropertyImportMode.CAD_RECOVERY,
                    options=RefreshOptions(tax_year=2026, skip_download=True, skip_extract=True),
                    prepare_only=True,
                )
            )
        finally:
            duration = time.monotonic() - start_time
            _, peak_mem = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            if old_down is not None:
                settings.BCAD_DOWNLOAD_DIR = old_down
            if old_ext is not None:
                settings.BCAD_EXTRACT_DIR = old_ext
            if old_log is not None:
                settings.BCAD_LOG_DIR = old_log

        if result.candidate_id:
            try:
                candidate = ImportCandidate.objects.get(pk=result.candidate_id)
                with connection.cursor() as cursor:
                    cursor.execute(f'DROP SCHEMA IF EXISTS "{candidate.storage_schema}" CASCADE')
                candidate.delete()
            except ImportCandidate.DoesNotExist:
                pass

        return {
            "county": "brazos",
            "rows": rows,
            "duration": duration,
            "throughput": rows / duration if duration > 0 else 0.0,
            "peak_memory_mb": peak_mem / (1024 * 1024),
            "status": result.workflow_state,
        }


class Command(BaseCommand):
    help = "Benchmark county ETL candidate staging throughput and memory consumption."

    def add_arguments(self, parser):
        parser.add_argument(
            "--county",
            choices=("harris", "brazos"),
            required=True,
            help="County ETL pipeline to benchmark",
        )
        parser.add_argument(
            "--rows",
            type=int,
            default=5000,
            help="Number of synthetic records to stage (default: 5000)",
        )

    def handle(self, *args, **options):
        county = options["county"]
        rows = options["rows"]
        if rows <= 0:
            raise CommandError("Number of rows must be positive")

        self.stdout.write(f"Starting pipeline benchmark for {county} with {rows} rows...")

        if county == "harris":
            stats = _benchmark_harris(rows)
        elif county == "brazos":
            stats = _benchmark_brazos(rows)
        else:
            raise CommandError(f"Unsupported county: {county}")

        self.stdout.write(self.style.SUCCESS("Benchmark completed successfully:"))
        self.stdout.write(f"  County:      {stats['county']}")
        self.stdout.write(f"  Rows:        {stats['rows']}")
        self.stdout.write(f"  Duration:    {stats['duration']:.2f} s")
        self.stdout.write(f"  Throughput:  {stats['throughput']:.1f} rows/s")
        self.stdout.write(f"  Peak Memory: {stats['peak_memory_mb']:.2f} MB")
        self.stdout.write(f"  Status:      {stats['status']}")
