"""
ETL Pipeline Package for HCAD Data Processing

This package provides a modular, robust ETL (Extract, Transform, Load) pipeline
for processing Harris County Appraisal District property data.

Modules:
    config: Configuration management for data sources and settings
    download: Download manager with retry logic and validation
    extract: Archive extraction with streaming support
    transform: Data parsing, validation, and normalization
    load: Database loading with transaction safety
    orchestrator: Pipeline coordination and error handling
    logging: Structured logging infrastructure

Usage:
    from data.etl_pipeline import ETLOrchestrator, ETLConfig
    
    config = ETLConfig.from_env()
    orchestrator = ETLOrchestrator(config)
    orchestrator.execute()
"""

from .config import ETLConfig, DataSource
from .download import DownloadManager
from .extract import ExtractManager
from .transform import DataTransformer
from .load import LoadManager
from .orchestrator import ETLOrchestrator
from .logging import ETLLogger

__all__ = [
    'ETLConfig',
    'DataSource',
    'DownloadManager',
    'ExtractManager',
    'DataTransformer',
    'LoadManager',
    'ETLOrchestrator',
    'ETLLogger',
]

__version__ = '1.0.0'
