"""
ETL Pipeline Configuration Module

Provides configuration management for data sources, settings, and pipeline behavior.
Supports environment variables, settings files, and programmatic configuration.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any
import logging

from django.conf import settings


logger = logging.getLogger(__name__)


class DataSourceType(Enum):
    """Types of data sources supported by the ETL pipeline."""
    PROPERTY_DATA = "property_data"
    GIS_DATA = "gis_data"
    CODE_DESCRIPTIONS = "code_descriptions"
    HEARING_DATA = "hearing_data"


class FileFormat(Enum):
    """Supported file formats for data sources."""
    ZIP = "zip"
    TAR = "tar"
    TAR_GZ = "tar.gz"
    CSV = "csv"
    TXT = "txt"
    SHP = "shp"  # Shapefile


@dataclass
class DataSource:
    """Configuration for a single data source.
    
    Attributes:
        name: Human-readable name for the data source
        url_template: URL template with {year} placeholder
        filename: Expected filename after download
        source_type: Type of data this source contains
        file_format: Format of the downloaded file
        required: Whether this source is required for the pipeline
        checksum: Optional expected SHA256 checksum
        extract_patterns: Optional patterns to filter extraction
        priority: Processing priority (lower = higher priority)
    """
    name: str
    url_template: str
    filename: str
    source_type: DataSourceType
    file_format: FileFormat = FileFormat.ZIP
    required: bool = True
    checksum: Optional[str] = None
    extract_patterns: List[str] = field(default_factory=list)
    priority: int = 100
    
    def get_url(self, year: Optional[int] = None) -> str:
        """Get the actual URL for a specific year."""
        if year is None:
            year = datetime.now().year
        return self.url_template.format(year=year)
    
    def __post_init__(self):
        """Validate data source configuration."""
        if not self.name:
            raise ValueError("DataSource name cannot be empty")
        if not self.url_template:
            raise ValueError("DataSource url_template cannot be empty")
        if '{year}' not in self.url_template and 'GIS' not in self.url_template:
            logger.warning(
                f"DataSource {self.name} URL does not contain {{year}} placeholder"
            )


# Default HCAD data sources
DEFAULT_PROPERTY_SOURCES: List[DataSource] = [
    DataSource(
        name="Real Account Owner",
        url_template="https://download.hcad.org/data/CAMA/{year}/Real_acct_owner.zip",
        filename="Real_acct_owner.zip",
        source_type=DataSourceType.PROPERTY_DATA,
        priority=10,
        extract_patterns=["real_acct.txt", "owners.txt", "deeds.txt"],
    ),
    DataSource(
        name="Real Account Ownership History",
        url_template="https://download.hcad.org/data/CAMA/{year}/Real_acct_ownership_history.zip",
        filename="Real_acct_ownership_history.zip",
        source_type=DataSourceType.PROPERTY_DATA,
        required=False,
        priority=90,
    ),
    DataSource(
        name="Real Building Land",
        url_template="https://download.hcad.org/data/CAMA/{year}/Real_building_land.zip",
        filename="Real_building_land.zip",
        source_type=DataSourceType.PROPERTY_DATA,
        priority=20,
        extract_patterns=["building_res.txt", "fixtures.txt", "extra_features.txt", "land.txt"],
    ),
    DataSource(
        name="Real Jur Exempt",
        url_template="https://download.hcad.org/data/CAMA/{year}/Real_jur_exempt.zip",
        filename="Real_jur_exempt.zip",
        source_type=DataSourceType.PROPERTY_DATA,
        required=False,
        priority=80,
    ),
    DataSource(
        name="Code Description Real",
        url_template="https://download.hcad.org/data/CAMA/{year}/Code_description_real.zip",
        filename="Code_description_real.zip",
        source_type=DataSourceType.CODE_DESCRIPTIONS,
        priority=5,
    ),
    DataSource(
        name="PP Files",
        url_template="https://download.hcad.org/data/CAMA/{year}/PP_files.zip",
        filename="PP_files.zip",
        source_type=DataSourceType.PROPERTY_DATA,
        required=False,
        priority=85,
    ),
    DataSource(
        name="Code Description PP",
        url_template="https://download.hcad.org/data/CAMA/{year}/Code_description_pp.zip",
        filename="Code_description_pp.zip",
        source_type=DataSourceType.CODE_DESCRIPTIONS,
        required=False,
        priority=6,
    ),
    DataSource(
        name="Hearing Files",
        url_template="https://download.hcad.org/data/CAMA/{year}/Hearing_files.zip",
        filename="Hearing_files.zip",
        source_type=DataSourceType.HEARING_DATA,
        required=False,
        priority=95,
    ),
]

DEFAULT_GIS_SOURCES: List[DataSource] = [
    DataSource(
        name="GIS Parcels",
        url_template="https://download.hcad.org/data/GIS/Parcels.zip",
        filename="Parcels.zip",
        source_type=DataSourceType.GIS_DATA,
        priority=50,
    ),
]


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
    chunk_size: int = 8192  # bytes
    max_parallel: int = 3
    verify_ssl: bool = True
    retry: RetryConfig = field(default_factory=RetryConfig)
    bandwidth_limit: Optional[int] = None  # bytes per second


@dataclass 
class ExtractConfig:
    """Configuration for extraction operations."""
    validate_archive: bool = True
    overwrite_existing: bool = True
    preserve_timestamps: bool = False
    max_file_size: Optional[int] = None  # bytes, for safety
    allowed_extensions: List[str] = field(
        default_factory=lambda: ['.txt', '.csv', '.shp', '.dbf', '.shx', '.prj', '.pdf']
    )


@dataclass
class TransformConfig:
    """Configuration for transform operations."""
    encoding_fallbacks: List[str] = field(
        default_factory=lambda: ['utf-8', 'latin-1', 'cp1252']
    )
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
    log_file_path: Optional[str] = None
    max_log_size: int = 10 * 1024 * 1024  # 10 MB
    backup_count: int = 5
    structured_logging: bool = True


@dataclass
class ETLConfig:
    """Main configuration class for the ETL pipeline.
    
    Aggregates all configuration sections and provides factory methods
    for creating configuration from various sources.
    """
    # Paths
    base_dir: Path = field(default_factory=lambda: Path(settings.BASE_DIR))
    download_dir: Path = field(default_factory=lambda: Path(settings.BASE_DIR) / 'downloads')
    extract_dir: Path = field(default_factory=lambda: Path(settings.BASE_DIR) / 'extracted')
    log_dir: Path = field(default_factory=lambda: Path(settings.BASE_DIR) / 'logs')
    
    # Data year
    data_year: int = field(default_factory=lambda: datetime.now().year)
    
    # Data sources
    property_sources: List[DataSource] = field(default_factory=lambda: DEFAULT_PROPERTY_SOURCES.copy())
    gis_sources: List[DataSource] = field(default_factory=lambda: DEFAULT_GIS_SOURCES.copy())
    
    # Component configs
    download: DownloadConfig = field(default_factory=DownloadConfig)
    extract: ExtractConfig = field(default_factory=ExtractConfig)
    transform: TransformConfig = field(default_factory=TransformConfig)
    load: LoadConfig = field(default_factory=LoadConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    
    # Pipeline behavior
    dry_run: bool = False
    skip_download: bool = False
    skip_extract: bool = False
    skip_transform: bool = False
    skip_load: bool = False
    continue_on_error: bool = True
    send_notifications: bool = False
    
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
    def from_env(cls) -> 'ETLConfig':
        """Create configuration from environment variables."""
        config = cls()
        
        # Override from environment
        data_year = os.getenv('ETL_DATA_YEAR')
        if data_year:
            config.data_year = int(data_year)
        
        download_dir = os.getenv('ETL_DOWNLOAD_DIR')
        if download_dir:
            config.download_dir = Path(download_dir)
        
        extract_dir = os.getenv('ETL_EXTRACT_DIR')
        if extract_dir:
            config.extract_dir = Path(extract_dir)
        
        if os.getenv('ETL_DRY_RUN', '').lower() in ('true', '1', 'yes'):
            config.dry_run = True
        
        batch_size = os.getenv('ETL_BATCH_SIZE')
        if batch_size:
            config.load.batch_size = int(batch_size)
        
        if os.getenv('ETL_LOW_MEMORY', '').lower() in ('true', '1', 'yes'):
            config.load.low_memory_mode = True
            config.load.batch_size = min(config.load.batch_size, 1000)
        
        log_level = os.getenv('ETL_LOG_LEVEL')
        if log_level:
            config.logging.level = log_level
        
        return config
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ETLConfig':
        """Create configuration from a dictionary."""
        config = cls()
        
        # Simple top-level fields
        for key in ['data_year', 'dry_run', 'skip_download', 'skip_extract', 
                    'skip_transform', 'skip_load', 'continue_on_error']:
            if key in data:
                setattr(config, key, data[key])
        
        # Path fields
        for key in ['download_dir', 'extract_dir', 'log_dir']:
            if key in data:
                setattr(config, key, Path(data[key]))
        
        # Nested configs
        if 'download' in data:
            for k, v in data['download'].items():
                if hasattr(config.download, k):
                    setattr(config.download, k, v)
        
        if 'load' in data:
            for k, v in data['load'].items():
                if hasattr(config.load, k):
                    setattr(config.load, k, v)
        
        return config
    
    def get_all_sources(self) -> List[DataSource]:
        """Get all data sources sorted by priority."""
        all_sources = self.property_sources + self.gis_sources
        return sorted(all_sources, key=lambda s: s.priority)
    
    def get_required_sources(self) -> List[DataSource]:
        """Get only required data sources."""
        return [s for s in self.get_all_sources() if s.required]
    
    def get_source_by_name(self, name: str) -> Optional[DataSource]:
        """Find a data source by name."""
        for source in self.get_all_sources():
            if source.name.lower() == name.lower():
                return source
        return None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to a dictionary for serialization."""
        return {
            'data_year': self.data_year,
            'download_dir': str(self.download_dir),
            'extract_dir': str(self.extract_dir),
            'log_dir': str(self.log_dir),
            'dry_run': self.dry_run,
            'skip_download': self.skip_download,
            'skip_extract': self.skip_extract,
            'skip_transform': self.skip_transform,
            'skip_load': self.skip_load,
            'continue_on_error': self.continue_on_error,
            'sources': [s.name for s in self.get_all_sources()],
        }
