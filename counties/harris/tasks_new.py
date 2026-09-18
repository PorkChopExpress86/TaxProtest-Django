"""
Celery tasks for HCAD data import using the new ETL pipeline.

These tasks provide async versions of the ETL pipeline operations
for use with Celery Beat scheduling and manual triggering.
"""

import logging
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from celery import shared_task
from django.conf import settings

from .etl_pipeline.import_plan import HarrisImportPlan
from .models import DownloadRecord
from .source_catalog import DEFAULT_HCAD_SOURCE_CATALOG

logger = logging.getLogger(__name__)


# =============================================================================
# Legacy Tasks (kept for backward compatibility during migration)
# =============================================================================

HCAD_ARCHIVE_SOURCES = DEFAULT_HCAD_SOURCE_CATALOG.legacy_archives()


def candidate_cama_years(reference_year: int | None = None) -> list[int]:
    """Return candidate CAMA data years, preferring the current year then previous year."""
    return DEFAULT_HCAD_SOURCE_CATALOG.candidate_years(reference_year or datetime.now().year)


def build_archive_candidate_urls(
    source: dict[str, Any], reference_year: int | None = None
) -> list[str]:
    """Build candidate download URLs for an HCAD archive source."""
    catalog_source = DEFAULT_HCAD_SOURCE_CATALOG.source_for_archive(source)
    return DEFAULT_HCAD_SOURCE_CATALOG.candidate_urls(catalog_source, reference_year)


def download_archive_with_fallback(
    source: dict[str, Any],
    download_dir: str,
    reference_year: int | None = None,
) -> tuple[str | None, str]:
    """Download an archive, falling back across candidate URLs when needed.

    Returns the URL that succeeded (or ``None`` for skipped optional archives)
    along with the local path used for the download.
    """
    filename = source["filename"]
    local_path = os.path.join(download_dir, filename)
    candidate_urls = build_archive_candidate_urls(source, reference_year)
    last_error: Exception | None = None

    for url in candidate_urls:
        try:
            with requests.get(url, stream=True, timeout=source.get("timeout", 300)) as r:
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    shutil.copyfileobj(r.raw, f)
            return url, local_path
        except requests.RequestException as exc:
            last_error = exc
            if os.path.exists(local_path):
                os.remove(local_path)

            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code == 404:
                logger.warning("Archive %s not available at %s", filename, url)
            else:
                logger.warning("Failed downloading %s from %s: %s", filename, url, exc)

    if source.get("required", True):
        if last_error:
            raise last_error
        raise FileNotFoundError(f"No candidate URLs succeeded for required archive {filename}")

    logger.warning("Skipping optional archive %s after trying %s", filename, candidate_urls)
    return None, local_path


def ensure_download_dir():
    download_dir = Path(settings.HCAD_DOWNLOAD_DIR)
    download_dir.mkdir(parents=True, exist_ok=True)
    return os.fspath(download_dir)


def ensure_extract_dir() -> str:
    extract_dir = Path(settings.HCAD_EXTRACT_DIR)
    extract_dir.mkdir(parents=True, exist_ok=True)
    return os.fspath(extract_dir)


@shared_task(bind=True)
def download_and_extract_hcad(self):
    """Download a set of HCAD ZIP files and extract them into the configured runtime dirs.

    Records a DownloadRecord per file.
    """
    download_dir = ensure_download_dir()
    extract_root = ensure_extract_dir()
    results = []
    reference_year = datetime.now().year

    for source in HCAD_ARCHIVE_SOURCES:
        local_name = source["filename"]
        downloaded_url, local_path = download_archive_with_fallback(
            source,
            download_dir,
            reference_year=reference_year,
        )

        if downloaded_url is None:
            results.append(
                {
                    "filename": local_name,
                    "url": None,
                    "local": local_path,
                    "extracted": False,
                    "optional": True,
                    "skipped": True,
                }
            )
            continue

        # create DB record
        rec = DownloadRecord.objects.create(url=downloaded_url, filename=local_name)

        # try to extract if zip
        try:
            if zipfile.is_zipfile(local_path):
                with zipfile.ZipFile(local_path, "r") as z:
                    extract_to = os.path.join(extract_root, local_name.replace(".zip", ""))
                    os.makedirs(extract_to, exist_ok=True)
                    z.extractall(extract_to)
                rec.extracted = True
                rec.save()
        except Exception as ex:
            if source.get("required", True):
                raise
            logger.warning(
                "Skipping optional archive %s after extraction error: %s", local_name, ex
            )

        results.append(
            {
                "filename": local_name,
                "url": downloaded_url,
                "local": local_path,
                "extracted": rec.extracted,
                "optional": not source.get("required", True),
                "skipped": False,
            }
        )

    return results


@shared_task(bind=True)
def download_and_import_building_data(self, actor=""):
    """Backward-compatible wrapper that now delegates to authoritative modern ETL."""
    return _run_authoritative_pipeline(
        task_instance=self,
        skip_download=False,
        skip_extract=False,
        skip_load=False,
        data_year=None,
        plan=HarrisImportPlan.from_legacy_scope("building-only"),
        strict=True,
        actor=actor,
    )


@shared_task(bind=True)
def download_and_import_gis_data(self, actor=""):
    """Backward-compatible wrapper that now delegates to authoritative modern ETL."""
    return _run_authoritative_pipeline(
        task_instance=self,
        skip_download=False,
        skip_extract=False,
        skip_load=False,
        data_year=None,
        plan=HarrisImportPlan.from_legacy_scope("gis-only"),
        strict=True,
        actor=actor,
    )


# =============================================================================
# New ETL Pipeline Tasks
# =============================================================================


def _run_authoritative_pipeline(
    *,
    task_instance: Any | None,
    skip_download: bool,
    skip_extract: bool,
    skip_load: bool,
    data_year: int | None,
    strict: bool,
    scope: str | None = None,
    plan: HarrisImportPlan | None = None,
    refresh_readiness: bool = True,
    validate_contract: bool | None = None,
    actor: str = "",
) -> dict[str, Any]:
    """Execute the authoritative modern ETL pipeline and propagate failures."""
    from .etl_pipeline import (
        ExtractedSourceRetention,
        HarrisAcquisitionMode,
        HarrisApply,
        HarrisExtractionMode,
        HarrisFailurePolicy,
        HarrisImportRequest,
        HarrisPreview,
        run_harris_import,
    )

    if validate_contract is None:
        validate_contract = not skip_load

    if task_instance is not None:
        task_instance.update_state(state="INITIALIZING", meta={"step": "Initializing pipeline"})

        class CeleryHarrisImportReporter:
            def report(self, event):
                task_instance.update_state(
                    state=event.phase.value.upper(),
                    meta={"step": event.message},
                )

        reporter = CeleryHarrisImportReporter()
    else:
        reporter = None

    resolved_plan = plan or HarrisImportPlan.from_legacy_scope(scope or "full")
    request = HarrisImportRequest(
        actor=actor,
        origin="celery" if task_instance is not None else "command",
        plan=resolved_plan,
        data_year=data_year,
        acquisition=(
            HarrisAcquisitionMode.REUSE_DOWNLOADED if skip_download else HarrisAcquisitionMode.FETCH
        ),
        extraction=(
            HarrisExtractionMode.REUSE_EXTRACTED if skip_extract else HarrisExtractionMode.EXTRACT
        ),
        load=(
            HarrisPreview()
            if skip_load
            else HarrisApply(
                refresh_readiness=refresh_readiness,
                validate_completeness=validate_contract,
                extracted_source_retention=ExtractedSourceRetention.REMOVE_AFTER_SUCCESS,
            )
        ),
        failure_policy=(HarrisFailurePolicy.STRICT if strict else HarrisFailurePolicy.BEST_EFFORT),
    )
    result = run_harris_import(request, reporter=reporter)

    result_dict = result.to_dict()
    if strict and not result.success:
        if task_instance is not None:
            task_instance.update_state(
                state="FAILURE",
                meta={"step": "Pipeline failed", "errors": result.errors[:3]},
            )
        errors = "; ".join(result.errors) if result.errors else "unknown failure"
        raise RuntimeError(f"Authoritative ETL pipeline failed: {errors}")
    if not strict and result.status.value == "failed":
        if task_instance is not None:
            task_instance.update_state(
                state="FAILURE",
                meta={"step": "Pipeline failed", "errors": result.errors[:3]},
            )
        errors = "; ".join(result.errors) if result.errors else "unknown failure"
        raise RuntimeError(f"Authoritative ETL pipeline failed: {errors}")

    if task_instance is not None:
        task_instance.update_state(state="SUCCESS", meta={"step": "Pipeline completed"})

    return result_dict


@shared_task(bind=True)
def run_etl_pipeline(
    self,
    skip_download: bool = False,
    skip_extract: bool = False,
    skip_load: bool = False,
    data_year: int | None = None,
    scope: str = "full",
    strict: bool = True,
    refresh_readiness: bool = True,
    validate_contract: bool | None = None,
    actor: str = "",
):
    """
    Run the full ETL pipeline using the new modular system.

    Args:
        skip_download: Skip downloading files (use existing)
        skip_extract: Skip extracting archives (use existing extracted files)
        skip_load: Skip loading to database (dry run)
        data_year: Override data year (default: current year)

    Returns:
        Dict with pipeline execution results
    """
    logger.info("Starting authoritative ETL pipeline task...")
    return _run_authoritative_pipeline(
        task_instance=self,
        skip_download=skip_download,
        skip_extract=skip_extract,
        skip_load=skip_load,
        data_year=data_year,
        scope=scope,
        strict=strict,
        refresh_readiness=refresh_readiness,
        validate_contract=validate_contract,
        actor=actor,
    )


@shared_task(bind=True)
def download_hcad_data(self, include_optional=False, data_year=None):
    """
    Download HCAD data files using the new ETL pipeline.

    Args:
        include_optional: Include optional data sources
        data_year: Override data year (default: current year)

    Returns:
        Dict with download results
    """
    from .etl_pipeline import DownloadManager, ETLConfig

    logger.info("Starting HCAD download task...")

    self.update_state(state="DOWNLOADING", meta={"step": "Downloading HCAD files"})

    config = ETLConfig.from_env()
    manager = DownloadManager(config, data_year=data_year)

    sources = (
        DEFAULT_HCAD_SOURCE_CATALOG.ordered_sources()
        if include_optional
        else DEFAULT_HCAD_SOURCE_CATALOG.required_sources()
    )

    results = manager.download_batch(sources)

    success_count = sum(1 for r in results if r.success)
    total_bytes = sum(r.bytes_downloaded for r in results)

    self.update_state(state="SUCCESS", meta={"step": "Download completed"})

    return {
        "sources_total": len(sources),
        "sources_success": success_count,
        "bytes_downloaded": total_bytes,
        "results": [
            {
                "name": r.source.name,
                "success": r.success,
                "bytes": r.bytes_downloaded,
                "error": r.error,
            }
            for r in results
        ],
    }


@shared_task(bind=True)
def extract_hcad_data(self):
    """
    Extract downloaded HCAD archives using the new ETL pipeline.

    Returns:
        Dict with extraction results
    """
    from .etl_pipeline import DownloadManager, ETLConfig, ExtractManager

    logger.info("Starting HCAD extraction task...")

    self.update_state(state="EXTRACTING", meta={"step": "Extracting HCAD archives"})

    config = ETLConfig.from_env()

    download_manager = DownloadManager(config)
    extract_manager = ExtractManager(config)

    # Only extract downloaded sources
    sources = [
        source
        for source in DEFAULT_HCAD_SOURCE_CATALOG.ordered_sources()
        if download_manager.is_downloaded(source)
    ]

    results = extract_manager.extract_batch(sources)

    success_count = sum(1 for r in results if r.success)
    total_files = sum(len(r.files_extracted or []) for r in results)

    self.update_state(state="SUCCESS", meta={"step": "Extraction completed"})

    return {
        "archives_total": len(sources),
        "archives_success": success_count,
        "files_extracted": total_files,
        "results": [
            {
                "name": r.source.name,
                "success": r.success,
                "files": len(r.files_extracted or []),
                "error": r.error,
            }
            for r in results
        ],
    }
