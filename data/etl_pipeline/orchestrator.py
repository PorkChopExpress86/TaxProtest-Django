"""
ETL Pipeline Orchestrator

Coordinates all ETL stages with error handling, metrics collection,
and notification support.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import ETLConfig, DataSource, DataSourceType
from .download import DownloadManager, DownloadResult
from .extract import ExtractManager, ExtractResult
from .transform import DataTransformer, TransformResult, get_schema
from .load import LoadManager, LoadResult
from .model_loader import ModelLoader, ModelLoadResult
from .logging import ETLLogger, ETLMetrics


class PipelineStage(Enum):
    """ETL pipeline stages."""
    DOWNLOAD = "download"
    EXTRACT = "extract"
    TRANSFORM = "transform"
    LOAD = "load"
    CLEANUP = "cleanup"


class PipelineStatus(Enum):
    """Overall pipeline status."""
    NOT_STARTED = "not_started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


@dataclass
class StageResult:
    """Result for a single pipeline stage."""
    stage: PipelineStage
    success: bool
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def duration(self) -> float:
        if self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return 0.0


@dataclass
class PipelineResult:
    """Overall pipeline execution result."""
    status: PipelineStatus
    started_at: datetime
    completed_at: Optional[datetime] = None
    stages: Dict[PipelineStage, StageResult] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
    @property
    def duration(self) -> float:
        if self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return 0.0
    
    @property
    def success(self) -> bool:
        return self.status == PipelineStatus.COMPLETED
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'status': self.status.value,
            'started_at': self.started_at.isoformat(),
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'duration_seconds': round(self.duration, 2),
            'stages': {
                stage.value: {
                    'success': result.success,
                    'duration': round(result.duration, 2),
                    'error': result.error,
                    'metrics': result.metrics,
                }
                for stage, result in self.stages.items()
            },
            'errors': self.errors,
            'warnings': self.warnings,
        }


class ETLOrchestrator:
    """Coordinates all ETL stages with error handling and metrics.
    
    Features:
    - Stage-by-stage execution
    - Dependency management
    - Error handling and recovery
    - Comprehensive logging
    - Metrics collection
    - Notification system
    - Support for skip/resume
    """
    
    def __init__(
        self,
        config: Optional[ETLConfig] = None,
        logger: Optional[ETLLogger] = None,
    ):
        self.config = config or ETLConfig.from_env()
        self.logger = logger or ETLLogger(
            name='etl_orchestrator',
            log_dir=self.config.log_dir,
            log_level=self.config.logging.level,
        )
        
        # Initialize managers
        self.download_manager = DownloadManager(self.config, self.logger)
        self.extract_manager = ExtractManager(self.config, self.logger)
        self.transformer = DataTransformer(self.config, self.logger)
        self.load_manager = LoadManager(self.config, self.logger)
        self.model_loader = ModelLoader(self.config, self.logger)
        
        # Pipeline state
        self.current_stage: Optional[PipelineStage] = None
        self.result: Optional[PipelineResult] = None
        
        # Callbacks
        self._stage_callbacks: Dict[PipelineStage, List[Callable]] = {
            stage: [] for stage in PipelineStage
        }
        self._completion_callbacks: List[Callable[[PipelineResult], None]] = []
    
    def register_stage_callback(
        self,
        stage: PipelineStage,
        callback: Callable[[StageResult], None],
    ) -> None:
        """Register a callback for a specific stage completion."""
        self._stage_callbacks[stage].append(callback)
    
    def register_completion_callback(
        self,
        callback: Callable[[PipelineResult], None],
    ) -> None:
        """Register a callback for pipeline completion."""
        self._completion_callbacks.append(callback)
    
    def _run_stage_callbacks(self, stage: PipelineStage, result: StageResult) -> None:
        """Run callbacks for a stage."""
        for callback in self._stage_callbacks[stage]:
            try:
                callback(result)
            except Exception as e:
                self.logger.warning(f"Stage callback error: {e}")
    
    def _run_completion_callbacks(self, result: PipelineResult) -> None:
        """Run callbacks for pipeline completion."""
        for callback in self._completion_callbacks:
            try:
                callback(result)
            except Exception as e:
                self.logger.warning(f"Completion callback error: {e}")
    
    def execute(
        self,
        sources: Optional[List[DataSource]] = None,
        skip_download: Optional[bool] = None,
        skip_extract: Optional[bool] = None,
        skip_transform: Optional[bool] = None,
        skip_load: Optional[bool] = None,
    ) -> PipelineResult:
        """Execute the full ETL pipeline.
        
        Args:
            sources: Specific sources to process (default: all required)
            skip_download: Skip download stage
            skip_extract: Skip extract stage
            skip_transform: Skip transform stage
            skip_load: Skip load stage
        
        Returns:
            PipelineResult with execution status and metrics
        """
        # Apply overrides
        skip_download = skip_download if skip_download is not None else self.config.skip_download
        skip_extract = skip_extract if skip_extract is not None else self.config.skip_extract
        skip_transform = skip_transform if skip_transform is not None else self.config.skip_transform
        skip_load = skip_load if skip_load is not None else self.config.skip_load
        
        # Initialize result
        self.result = PipelineResult(
            status=PipelineStatus.RUNNING,
            started_at=datetime.now(),
        )
        
        sources = sources or self.config.get_required_sources()
        
        self.logger.info(
            f"Starting ETL pipeline for {len(sources)} sources "
            f"(year={self.config.data_year})"
        )
        
        try:
            # Download stage
            if not skip_download:
                stage_result = self._execute_download(sources)
                self.result.stages[PipelineStage.DOWNLOAD] = stage_result
                
                if not stage_result.success and not self.config.continue_on_error:
                    raise Exception("Download stage failed")
            
            # Extract stage
            if not skip_extract:
                stage_result = self._execute_extract(sources)
                self.result.stages[PipelineStage.EXTRACT] = stage_result
                
                if not stage_result.success and not self.config.continue_on_error:
                    raise Exception("Extract stage failed")
            
            # Transform and Load stages (combined for efficiency)
            if not skip_transform or not skip_load:
                stage_result = self._execute_transform_load(
                    sources,
                    skip_transform=skip_transform,
                    skip_load=skip_load,
                )
                self.result.stages[PipelineStage.LOAD] = stage_result
                
                if not stage_result.success and not self.config.continue_on_error:
                    raise Exception("Transform/Load stage failed")
            
            # Determine final status
            all_success = all(r.success for r in self.result.stages.values())
            self.result.status = PipelineStatus.COMPLETED if all_success else PipelineStatus.PARTIAL
            
        except Exception as e:
            self.result.status = PipelineStatus.FAILED
            self.result.errors.append(str(e))
            self.logger.exception("Pipeline execution failed")
        
        finally:
            self.result.completed_at = datetime.now()
            self._run_completion_callbacks(self.result)
            
            self.logger.info(
                f"Pipeline {self.result.status.value}: "
                f"{self.result.duration:.1f}s total"
            )
        
        return self.result
    
    def _execute_download(self, sources: List[DataSource]) -> StageResult:
        """Execute download stage."""
        stage_result = StageResult(stage=PipelineStage.DOWNLOAD, success=True)
        self.current_stage = PipelineStage.DOWNLOAD
        
        self.logger.info(f"Download stage: {len(sources)} sources")
        
        with self.logger.stage("download") as metrics:
            results = self.download_manager.download_batch(sources)
            
            # Collect metrics
            success_count = sum(1 for r in results if r.success)
            total_bytes = sum(r.bytes_downloaded for r in results)
            
            metrics.records_processed = len(results)
            metrics.records_success = success_count
            metrics.records_failed = len(results) - success_count
            metrics.bytes_downloaded = total_bytes
            
            stage_result.metrics = {
                'sources_total': len(sources),
                'sources_success': success_count,
                'bytes_downloaded': total_bytes,
            }
            
            if success_count < len(sources):
                failed = [r.source.name for r in results if not r.success]
                stage_result.error = f"Failed downloads: {', '.join(failed)}"
                
                # Only fail if required sources failed
                required_failed = [
                    r for r in results 
                    if not r.success and r.source.required
                ]
                stage_result.success = len(required_failed) == 0
        
        stage_result.completed_at = datetime.now()
        self._run_stage_callbacks(PipelineStage.DOWNLOAD, stage_result)
        return stage_result
    
    def _execute_extract(self, sources: List[DataSource]) -> StageResult:
        """Execute extract stage."""
        stage_result = StageResult(stage=PipelineStage.EXTRACT, success=True)
        self.current_stage = PipelineStage.EXTRACT
        
        self.logger.info(f"Extract stage: {len(sources)} archives")
        
        with self.logger.stage("extract") as metrics:
            results = self.extract_manager.extract_batch(sources)
            
            success_count = sum(1 for r in results if r.success)
            total_bytes = sum(r.bytes_extracted for r in results)
            total_files = sum(len(r.files_extracted or []) for r in results)
            
            metrics.records_processed = len(results)
            metrics.records_success = success_count
            metrics.bytes_extracted = total_bytes
            
            stage_result.metrics = {
                'archives_total': len(sources),
                'archives_success': success_count,
                'files_extracted': total_files,
                'bytes_extracted': total_bytes,
            }
            
            if success_count < len(sources):
                failed = [r.source.name for r in results if not r.success]
                stage_result.error = f"Failed extractions: {', '.join(failed)}"
                
                required_failed = [
                    r for r in results 
                    if not r.success and r.source.required
                ]
                stage_result.success = len(required_failed) == 0
        
        stage_result.completed_at = datetime.now()
        self._run_stage_callbacks(PipelineStage.EXTRACT, stage_result)
        return stage_result
    
    def _preload_fixtures(self, sources: List[DataSource]) -> None:
        """
        Pre-load fixtures.txt to extract bedroom/bathroom counts.
        
        This must be called before processing building_res.txt so that
        bedroom/bathroom data is available during building loading.
        
        Args:
            sources: List of data sources to search for fixtures.txt
        """
        self.logger.info("Pre-loading fixtures for bedroom/bathroom data...")
        
        # Find the Real Building Land source
        for source in sources:
            if source.name == "Real Building Land":
                extract_path = self.extract_manager.get_extract_path(source)
                fixtures_path = extract_path / 'fixtures.txt'
                
                if fixtures_path.exists():
                    try:
                        self.model_loader.fixtures_aggregator.load_fixtures_file(fixtures_path)
                        
                        # Log statistics
                        stats = self.model_loader.fixtures_aggregator.get_stats()
                        self.logger.info(
                            f"Fixtures loaded: {stats['total_buildings']:,} buildings, "
                            f"{stats['with_bedrooms']:,} with bedrooms, "
                            f"{stats['with_bathrooms']:,} with bathrooms"
                        )
                        return
                    except Exception as e:
                        self.logger.error(f"Error loading fixtures: {e}")
                        # Continue without fixtures - fields will be NULL
                        return
                else:
                    self.logger.warning(f"Fixtures file not found: {fixtures_path}")
                    return
        
        self.logger.warning("Real Building Land source not found in sources list")
    
    def _execute_transform_load(
        self,
        sources: List[DataSource],
        skip_transform: bool = False,
        skip_load: bool = False,
    ) -> StageResult:
        """Execute transform and load stages.
        
        These are combined for memory efficiency - records are streamed
        from transform directly to load without buffering.
        """
        stage_result = StageResult(stage=PipelineStage.LOAD, success=True)
        self.current_stage = PipelineStage.LOAD
        
        self.logger.info("Transform/Load stage")
        
        with self.logger.stage("transform_load") as metrics:
            total_loaded = 0
            total_failed = 0
            gis_loaded = 0
            
            # STEP 1: Pre-load fixtures for bedroom/bathroom data
            # This must happen before processing building_res.txt
            self._preload_fixtures(sources)
            
            # Process each source type
            for source in sources:
                if source.source_type == DataSourceType.GIS_DATA:
                    # GIS data requires special handling
                    gis_result = self._process_gis_source(source)
                    gis_loaded += gis_result.get('loaded', 0)
                    total_failed += gis_result.get('failed', 0)
                    continue
                
                # Find extracted files for this source
                extract_path = self.extract_manager.get_extract_path(source)
                if not extract_path.exists():
                    self.logger.warning(f"Extract path not found: {extract_path}")
                    continue
                
                # Process each data file
                for file_path in extract_path.rglob('*.txt'):
                    result = self._process_data_file(file_path, skip_load)
                    total_loaded += result.get('loaded', 0)
                    total_failed += result.get('failed', 0)
            
            metrics.records_processed = total_loaded + total_failed
            metrics.records_success = total_loaded
            metrics.records_failed = total_failed
            
            stage_result.metrics = {
                'records_loaded': total_loaded,
                'records_failed': total_failed,
                'gis_coordinates_updated': gis_loaded,
            }
            
            if total_failed > 0:
                stage_result.error = f"{total_failed} records failed to load"
        
        stage_result.completed_at = datetime.now()
        self._run_stage_callbacks(PipelineStage.LOAD, stage_result)
        return stage_result
    
    def _process_data_file(
        self,
        file_path: Path,
        skip_load: bool = False,
        truncate: bool = True,
    ) -> Dict[str, int]:
        """Process a single data file.
        
        Args:
            file_path: Path to the data file
            skip_load: If True, only transform without loading to database
            truncate: If True, truncate the table before loading
        
        Returns:
            Dictionary with 'loaded' and 'failed' counts
        """
        # Determine schema based on filename
        filename = file_path.stem.lower()
        schema_name = None
        
        # Skip code description files (lookup tables, not actual data)
        if filename.startswith('desc_'):
            self.logger.debug(f"Skipping code description file: {file_path.name}")
            return {'loaded': 0, 'failed': 0}
        
        # Match exact filenames for data files
        if filename == 'real_acct':
            schema_name = 'real_acct'
        elif filename == 'building_res':
            schema_name = 'building_res'
        elif filename == 'extra_features':
            schema_name = 'extra_features'
        
        if not schema_name:
            self.logger.debug(f"No schema for {file_path.name}, skipping")
            return {'loaded': 0, 'failed': 0}
        
        schema = get_schema(schema_name)
        if not schema:
            return {'loaded': 0, 'failed': 0}
        
        self.logger.info(f"Processing {file_path.name} with schema {schema_name}")
        
        if skip_load:
            # Just transform and count records without loading
            records = list(self.transformer.iter_records(file_path, schema))
            return {'loaded': len(records), 'failed': 0}
        
        # Transform and load records to Django models
        # Filter out None values from the generator
        records_gen = (r for r in self.transformer.iter_records(file_path, schema) if r is not None)
        
        if schema_name == 'real_acct':
            result = self.model_loader.load_property_records(
                records_gen, truncate=truncate
            )
        elif schema_name == 'building_res':
            result = self.model_loader.load_building_details(
                records_gen, truncate=truncate
            )
        elif schema_name == 'extra_features':
            result = self.model_loader.load_extra_features(
                records_gen, truncate=truncate
            )
        else:
            return {'loaded': 0, 'failed': 0}
        
        return {
            'loaded': result.records_loaded,
            'failed': result.records_invalid + result.records_skipped,
        }
    
    def _process_gis_source(self, source: DataSource) -> Dict[str, int]:
        """Process GIS data source.
        
        Finds shapefiles in the extracted GIS data and loads coordinates
        into PropertyRecord latitude/longitude fields.
        
        Args:
            source: The GIS data source configuration
            
        Returns:
            Dictionary with 'loaded' and 'failed' counts
        """
        self.logger.info(f"Processing GIS source: {source.name}")
        
        # Get the extract path for GIS data
        extract_path = self.extract_manager.get_extract_path(source)
        if not extract_path.exists():
            self.logger.warning(f"GIS extract path not found: {extract_path}")
            return {'loaded': 0, 'failed': 0}
        
        # Find shapefile(s) in extracted directory
        shapefiles = list(extract_path.rglob('*.shp'))
        if not shapefiles:
            self.logger.warning(f"No shapefiles found in {extract_path}")
            return {'loaded': 0, 'failed': 0}
        
        # Prefer ParcelsCity.shp if available (primary parcel data)
        shapefile_path = None
        for shp in shapefiles:
            if 'ParcelsCity' in shp.name:
                shapefile_path = shp
                break
        
        # Fall back to first shapefile found
        if shapefile_path is None:
            shapefile_path = shapefiles[0]
        
        self.logger.info(f"Loading GIS data from: {shapefile_path}")
        
        try:
            # Import and call the GIS loading function
            from data.etl import load_gis_parcels
            
            count = load_gis_parcels(str(shapefile_path))
            self.logger.info(f"Updated {count} properties with GIS coordinates")
            return {'loaded': count, 'failed': 0}
            
        except ImportError as e:
            self.logger.error(f"GIS processing requires geopandas: {e}")
            return {'loaded': 0, 'failed': 1}
        except Exception as e:
            self.logger.exception(f"Error processing GIS data: {e}")
            return {'loaded': 0, 'failed': 1}
    
    def execute_download_only(
        self,
        sources: Optional[List[DataSource]] = None,
        include_optional: bool = False,
    ) -> List[DownloadResult]:
        """Execute only the download stage."""
        if sources is None:
            if include_optional:
                sources = self.config.get_all_sources()
            else:
                sources = self.config.get_required_sources()
        
        return self.download_manager.download_batch(sources)
    
    def execute_extract_only(
        self,
        sources: Optional[List[DataSource]] = None,
    ) -> List[ExtractResult]:
        """Execute only the extract stage."""
        sources = sources or self.config.get_all_sources()
        return self.extract_manager.extract_batch(sources)
    
    def cleanup(
        self,
        remove_downloads: bool = False,
        remove_extracts: bool = True,
    ) -> None:
        """Clean up temporary files."""
        self.logger.info("Running cleanup")
        
        if remove_downloads:
            self.download_manager.cleanup()
        
        if remove_extracts:
            self.extract_manager.cleanup()
    
    def get_status(self) -> Dict[str, Any]:
        """Get current pipeline status."""
        return {
            'current_stage': self.current_stage.value if self.current_stage else None,
            'result': self.result.to_dict() if self.result else None,
            'config': {
                'data_year': self.config.data_year,
                'dry_run': self.config.dry_run,
                'continue_on_error': self.config.continue_on_error,
            },
        }
