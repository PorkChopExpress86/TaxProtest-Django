"""Harris operator benchmark: a synthetic property source staged as a prepare-only import."""

from __future__ import annotations

import tempfile
from pathlib import Path

from counties.common.benchmarking import BenchmarkStats, measure, swapped_settings
from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisExtractionMode,
    HarrisImportRequest,
    HarrisPrepare,
    run_harris_import,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan


def write_synthetic_real_acct(root: str | Path, count: int) -> Path:
    """Write ``count`` residential accounts to ``<root>/extracted/Real_acct_owner/real_acct.txt``."""
    source_dir = Path(root) / "extracted" / "Real_acct_owner"
    source_dir.mkdir(parents=True, exist_ok=True)
    source = source_dir / "real_acct.txt"
    lines = ["acct\tsite_addr_1\tsite_addr_3\tstate_class\ttot_appr_val"]
    for i in range(count):
        acct = f"P{i:07d}"
        lines.append(f"{acct}\t{i} MAIN ST\t77001\tA1\t{250000 + (i % 50000)}")
    source.write_text("\n".join(lines) + "\n", encoding="latin-1")
    return source


def benchmark_harris(rows: int) -> BenchmarkStats:
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
        write_synthetic_real_acct(base, rows)

        with swapped_settings(
            BASE_DIR=str(base),
            HCAD_DOWNLOAD_DIR=str(base / "downloads"),
            HCAD_EXTRACT_DIR=str(base / "extracted"),
            HCAD_LOG_DIR=str(base / "logs"),
        ):
            return measure(
                "harris",
                rows=rows,
                run=lambda: run_harris_import(request),
                status=lambda result: result.status.name,
            )
