"""Brazos operator benchmark: a synthetic PACS export staged as a prepare-only CAD recovery."""

from __future__ import annotations

import tempfile
from pathlib import Path

from counties.brazos.cad_refresh import (
    ENTITY_INFO_FILENAME,
    IMPROVEMENT_DETAIL_ATTR_FILENAME,
    IMPROVEMENT_DETAIL_FILENAME,
    CadRefreshStage,
)
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
    RefreshOptions,
)
from counties.common.benchmarking import BenchmarkStats, measure, swapped_settings


def _line(length: int, fields: dict[tuple[int, int], str]) -> str:
    chars = [" "] * length
    for (start, end), value in fields.items():
        chars[start : start + len(value[: end - start])] = value[: end - start]
    return "".join(chars)


def write_complete_pacs_export(root: Path, year: int) -> None:
    """Write one keyed record for every PACS layout the preflight consumes."""
    prop_id = "000000010013"
    extract_dir = root / "extracted" / str(year)
    extract_dir.mkdir(parents=True)
    files = {
        "APPRAISAL_INFO.TXT": _line(987, {(0, 12): prop_id, (17, 22): f"0{year}"}),
        "APPRAISAL_LAND_DETAIL.TXT": _line(184, {(0, 12): prop_id, (12, 16): str(year)}),
        "APPRAISAL_IMPROVEMENT_INFO.TXT": _line(
            49, {(0, 12): prop_id, (12, 16): str(year), (16, 28): "000000000001"}
        ),
        IMPROVEMENT_DETAIL_FILENAME: _line(
            622, {(0, 12): prop_id, (12, 16): str(year), (16, 28): "000000000001"}
        ),
        IMPROVEMENT_DETAIL_ATTR_FILENAME: _line(
            87, {(0, 12): prop_id, (12, 16): str(year), (16, 28): "000000000001"}
        ),
        ENTITY_INFO_FILENAME: _line(
            418,
            {
                (0, 12): prop_id,
                (12, 17): f"0{year}",
                (53, 63): "G1",
            },
        ),
    }
    for filename, contents in files.items():
        (extract_dir / filename).write_text(contents + "\n", encoding="utf-8")


def write_synthetic_pacs(root: Path, count: int = 5000, year: int = 2026) -> None:
    """Write a complete PACS export repeated for ``count`` distinct property identities."""
    write_complete_pacs_export(root, year)
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


def benchmark_brazos(rows: int) -> BenchmarkStats:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "downloads").mkdir(parents=True, exist_ok=True)
        (root / "logs").mkdir(parents=True, exist_ok=True)
        write_synthetic_pacs(root, count=rows, year=2026)

        request = PropertyImportRequest(
            mode=PropertyImportMode.CAD_RECOVERY,
            options=RefreshOptions(tax_year=2026, skip_download=True, skip_extract=True),
            prepare_only=True,
        )
        with swapped_settings(
            BCAD_DOWNLOAD_DIR=str(root / "downloads"),
            BCAD_EXTRACT_DIR=str(root / "extracted"),
            BCAD_LOG_DIR=str(root / "logs"),
        ):
            return measure(
                "brazos",
                rows=rows,
                run=lambda: BrazosPropertyImport(CadRefreshStage(), None).run(request),
                status=lambda result: result.workflow_state,
            )
