"""
ETL Pipeline Configuration Module

Provides paths and operational tuning for Harris ETL components. Import intent,
source selection, failure policy, and cleanup policy live outside this module.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings

from counties.harris.source_catalog import DataSource, DataSourceType, FileFormat

__all__ = ["DataSource", "DataSourceType", "ETLConfig", "FileFormat"]


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_retries: int = 3
    initial_delay: float = 1.0  # seconds
    max_delay: float = 60.0  # seconds
    exponential_base: float = 2.0
    jitter: bool = True


@dataclass
class DownloadConfig:
    """Configuration for download operations."""

    timeout: int = 300  # seconds
    chunk_size: int = 1 << 20  # 1 MiB — fewer write syscalls on multi-GB archives
    max_parallel: int = 3
    verify_ssl: bool = True
    retry: RetryConfig = field(default_factory=RetryConfig)
    bandwidth_limit: int | None = None  # bytes per second
    # Skip re-downloading a file when the remote size + Last-Modified match the
    # local copy. Avoids re-pulling unchanged multi-GB archives every run.
    skip_if_unchanged: bool = True


@dataclass
class ExtractConfig:
    """Configuration for extraction operations."""

    validate_archive: bool = True
    overwrite_existing: bool = True
    preserve_timestamps: bool = False
    max_file_size: int | None = None  # bytes, for safety
    allowed_extensions: list[str] = field(
        default_factory=lambda: [".txt", ".csv", ".shp", ".dbf", ".shx", ".prj", ".pdf"]
    )


@dataclass
class TransformConfig:
    """Configuration for transform operations."""

    encoding_fallbacks: list[str] = field(default_factory=lambda: ["utf-8", "latin-1", "cp1252"])
    skip_invalid_records: bool = True
    max_errors_before_abort: int = 1000
    normalize_whitespace: bool = True
    strip_fields: bool = True


@dataclass
class LoadConfig:
    """Configuration for load operations."""

    batch_size: int = 5000
    use_transactions: bool = True
    truncate_before_load: bool = True
    checkpoint_interval: int = 10000
    max_retries_per_batch: int = 2
    low_memory_mode: bool = False


@dataclass
class LoggingConfig:
    """Configuration for logging behavior."""

    level: str = "INFO"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    log_to_file: bool = True
    log_file_path: str | None = None
    max_log_size: int = 10 * 1024 * 1024  # 10 MB
    backup_count: int = 5
    structured_logging: bool = True


@dataclass
class ETLConfig:
    """Infrastructure configuration shared by Harris ETL components."""

    # Paths
    base_dir: Path = field(default_factory=lambda: Path(settings.BASE_DIR))
    download_dir: Path = field(default_factory=lambda: Path(settings.HCAD_DOWNLOAD_DIR))
    extract_dir: Path = field(default_factory=lambda: Path(settings.HCAD_EXTRACT_DIR))
    log_dir: Path = field(default_factory=lambda: Path(settings.HCAD_LOG_DIR))

    # Component configs
    download: DownloadConfig = field(default_factory=DownloadConfig)
    extract: ExtractConfig = field(default_factory=ExtractConfig)
    transform: TransformConfig = field(default_factory=TransformConfig)
    load: LoadConfig = field(default_factory=LoadConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def __post_init__(self):
        """Ensure directories exist and validate configuration."""
        self.download_dir = Path(self.download_dir)
        self.extract_dir = Path(self.extract_dir)
        self.log_dir = Path(self.log_dir)

        # Create directories
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.extract_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "ETLConfig":
        """Create configuration from environment variables."""
        config = cls()

        # Override from environment
        download_dir = os.getenv("ETL_DOWNLOAD_DIR")
        if download_dir:
            config.download_dir = Path(download_dir)

        extract_dir = os.getenv("ETL_EXTRACT_DIR")
        if extract_dir:
            config.extract_dir = Path(extract_dir)

        if os.getenv("ETL_FORCE_DOWNLOAD", "").lower() in ("true", "1", "yes"):
            config.download.skip_if_unchanged = False

        batch_size = os.getenv("ETL_BATCH_SIZE")
        if batch_size:
            config.load.batch_size = int(batch_size)

        if os.getenv("ETL_LOW_MEMORY", "").lower() in ("true", "1", "yes"):
            config.load.low_memory_mode = True
            config.load.batch_size = min(config.load.batch_size, 1000)

        log_level = os.getenv("ETL_LOG_LEVEL")
        if log_level:
            config.logging.level = log_level

        return config

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ETLConfig":
        """Create configuration from a dictionary."""
        config = cls()

        # Simple top-level fields
        # Path fields
        for key in ["download_dir", "extract_dir", "log_dir"]:
            if key in data:
                setattr(config, key, Path(data[key]))

        # Nested configs
        if "download" in data:
            for k, v in data["download"].items():
                if hasattr(config.download, k):
                    setattr(config.download, k, v)

        if "load" in data:
            for k, v in data["load"].items():
                if hasattr(config.load, k):
                    setattr(config.load, k, v)

        return config

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration to a dictionary for serialization."""
        return {
            "download_dir": str(self.download_dir),
            "extract_dir": str(self.extract_dir),
            "log_dir": str(self.log_dir),
        }
