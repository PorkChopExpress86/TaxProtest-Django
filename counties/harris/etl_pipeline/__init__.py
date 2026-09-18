"""
ETL Pipeline Package for HCAD Data Processing

This package provides a modular, robust ETL (Extract, Transform, Load) pipeline
for processing Harris County Appraisal District property data.

Modules:
    config: Configuration management for data sources and settings
    download: Download manager with retry logic and validation
    extract: Archive extraction with streaming support
    transform: Data parsing, validation, and normalization
    persistence: Authoritative translated-row persistence and database adapters
    source_pipeline: Stateless tabular transformation and persistence pipeline
    orchestrator: Deep, stateless Harris import boundary
    logging: Structured logging infrastructure

Usage:
    from counties.harris.etl_pipeline import HarrisImportPlan, HarrisImportRequest
    from counties.harris.etl_pipeline import run_harris_import

    result = run_harris_import(
        HarrisImportRequest(plan=HarrisImportPlan.from_legacy_scope("full"))
    )
"""

from .config import DataSource, ETLConfig
from .download import DownloadManager
from .extract import ExtractManager
from .import_plan import HarrisImportPlan
from .logging import ETLLogger
from .orchestrator import (
    ExtractedSourceRetention,
    HarrisAcquisitionMode,
    HarrisApply,
    HarrisExtractionMode,
    HarrisFailurePolicy,
    HarrisImportEvent,
    HarrisImportPhase,
    HarrisImportReporter,
    HarrisImportRequest,
    HarrisImportResult,
    HarrisImportStageResult,
    HarrisImportStatus,
    HarrisLoadIntent,
    HarrisPrepare,
    HarrisPreview,
    HarrisPropertyFile,
    InvalidHarrisImportRequest,
    run_harris_import,
)
from .source_pipeline import HarrisSourcePipeline, resolve_schema_name
from .transform import DataTransformer

__all__ = [
    "ETLConfig",
    "DataSource",
    "DownloadManager",
    "ExtractManager",
    "DataTransformer",
    "ETLLogger",
    "HarrisImportPlan",
    "HarrisSourcePipeline",
    "resolve_schema_name",
    "ExtractedSourceRetention",
    "HarrisAcquisitionMode",
    "HarrisApply",
    "HarrisPrepare",
    "HarrisPropertyFile",
    "HarrisExtractionMode",
    "HarrisFailurePolicy",
    "HarrisImportEvent",
    "HarrisImportPhase",
    "HarrisImportReporter",
    "HarrisImportRequest",
    "HarrisImportResult",
    "HarrisImportStageResult",
    "HarrisImportStatus",
    "HarrisLoadIntent",
    "HarrisPreview",
    "InvalidHarrisImportRequest",
    "run_harris_import",
]

__version__ = "1.0.0"
