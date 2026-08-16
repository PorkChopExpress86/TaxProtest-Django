"""
ETL Pipeline Package for HCAD Data Processing

This package provides a modular, robust ETL (Extract, Transform, Load) pipeline
for processing Harris County Appraisal District property data.

Modules:
    config: Configuration management for data sources and settings
    download: Download manager with retry logic and validation
    extract: Archive extraction with streaming support
    row_reader: Source-file parsing into typed rows (single source of truth)
    model_loader: Django model loading for PropertyRecord, BuildingDetail, ExtraFeature
    orchestrator: Pipeline coordination and error handling
    logging: Structured logging infrastructure

Usage:
    from counties.harris.etl_pipeline import ETLOrchestrator, ETLConfig

    config = ETLConfig.from_env()
    orchestrator = ETLOrchestrator(config)
    orchestrator.execute()
"""

from .config import DataSource, ETLConfig
from .download import DownloadManager
from .extract import ExtractManager
from .logging import ETLLogger
from .model_loader import ModelLoader
from .orchestrator import ETLOrchestrator

__all__ = [
    "ETLConfig",
    "DataSource",
    "DownloadManager",
    "ExtractManager",
    "ModelLoader",
    "ETLOrchestrator",
    "ETLLogger",
]

__version__ = "1.0.0"
