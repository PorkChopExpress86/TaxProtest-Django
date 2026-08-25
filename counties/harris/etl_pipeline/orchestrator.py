"""
ETL Pipeline Orchestrator

Coordinates all ETL stages with strict validation, robust error handling,
and metrics collection.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from django.core.management import call_command
from django.core.management.base import CommandError as DjangoCommandError

from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, HcadSourceId

from .config import DataSource, DataSourceType, ETLConfig
from .download import DownloadManager, DownloadResult
from .extract import ExtractManager, ExtractResult
from .import_plan import HarrisImportPlan
from .logging import ETLLogger
from .model_loader import ModelLoader
from .row_reader import RowResult, iter_building_rows, iter_extra_feature_rows, iter_property_rows
from .transform import DataTransformer


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
    completed_at: datetime | None = None
    error: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

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
    completed_at: datetime | None = None
    stages: dict[PipelineStage, StageResult] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        if self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return 0.0

    @property
    def success(self) -> bool:
        return self.status == PipelineStatus.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "status": self.status.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": round(self.duration, 2),
            "stages": {
                stage.value: {
                    "success": result.success,
                    "duration": round(result.duration, 2),
                    "error": result.error,
                    "metrics": result.metrics,
                }
                for stage, result in self.stages.items()
            },
            "errors": self.errors,
            "warnings": self.warnings,
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
        config: ETLConfig | None = None,
        logger: ETLLogger | None = None,
    ):
        self.config = config or ETLConfig.from_env()
        self.logger = logger or ETLLogger(
            name="etl_orchestrator",
            log_dir=self.config.log_dir,
            log_level=self.config.logging.level,
        )

        # Initialize managers
        self.download_manager = DownloadManager(self.config, self.logger)
        self.extract_manager = ExtractManager(self.config, self.logger)
        # Kept for direct callers of the generic transformer. The core Harris
        # import path below uses row_reader for all three property-data files.
        self.transformer = DataTransformer(self.config, self.logger)
        self.model_loader = ModelLoader(self.config, self.logger)

        # Pipeline state
        self.current_stage: PipelineStage | None = None
        self.result: PipelineResult | None = None

        # Callbacks
        self._stage_callbacks: dict[PipelineStage, list[Callable]] = {
            stage: [] for stage in PipelineStage
        }
        self._completion_callbacks: list[Callable[[PipelineResult], None]] = []

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
        sources: list[DataSource] | None = None,
        plan: HarrisImportPlan | None = None,
        skip_download: bool | None = None,
        skip_extract: bool | None = None,
        skip_transform: bool | None = None,
        skip_load: bool | None = None,
        cleanup_extracted: bool | None = None,
        scope: str | None = None,
        strict: bool = True,
        validate_contract: bool = True,
        refresh_readiness: bool = True,
    ) -> PipelineResult:
        """Execute the full ETL pipeline.

        Args:
            sources: Specific sources to process (default: all required)
            plan: Harris import intent that owns source selection and validation skips
            skip_download: Skip download stage
            skip_extract: Skip extract stage
            skip_transform: Skip transform stage
            skip_load: Skip load stage
            cleanup_extracted: Delete uncompressed extracted files after successful load
            scope: Deprecated compatibility spelling for a Harris import plan
            strict: Fail run on required gaps/errors
            validate_contract: Run post-load validate_data checks
            refresh_readiness: Recompute readiness after a successful data write

        Returns:
            PipelineResult with execution status and metrics
        """
        plan = self._resolve_import_plan(plan=plan, scope=scope)

        # Apply overrides
        skip_download = skip_download if skip_download is not None else self.config.skip_download
        skip_extract = skip_extract if skip_extract is not None else self.config.skip_extract
        skip_transform = (
            skip_transform if skip_transform is not None else self.config.skip_transform
        )
        skip_load = skip_load if skip_load is not None else self.config.skip_load
        skip_load = bool(skip_load or self.config.dry_run)
        cleanup_extracted = (
            cleanup_extracted if cleanup_extracted is not None else self.config.cleanup_extracted
        )

        # Initialize result
        self.result = PipelineResult(
            status=PipelineStatus.RUNNING,
            started_at=datetime.now(),
        )

        sources = plan.select_sources(sources or self.config.get_required_sources())
        if not sources:
            raise ValueError(f"No ETL sources match Harris import plan {plan.legacy_scope}")

        self.logger.info(
            f"Starting ETL pipeline for {len(sources)} sources "
            f"(year={self.config.data_year}, plan={plan.legacy_scope}, "
            f"strict={strict}, dry_run={self.config.dry_run})"
        )

        try:
            # Download stage
            if not skip_download:
                stage_result = self._execute_download(sources)
                self.result.stages[PipelineStage.DOWNLOAD] = stage_result

                if not stage_result.success and strict:
                    raise RuntimeError("Download stage failed")

            # Extract stage
            if not skip_extract:
                stage_result = self._execute_extract(sources)
                self.result.stages[PipelineStage.EXTRACT] = stage_result

                if not stage_result.success and strict:
                    raise RuntimeError("Extract stage failed")

            # Transform and Load stages (combined for efficiency)
            if not skip_transform or not skip_load:
                stage_result = self._execute_transform_load(
                    sources,
                    skip_transform=skip_transform,
                    skip_load=skip_load,
                )
                self.result.stages[PipelineStage.LOAD] = stage_result

                if not stage_result.success and strict:
                    raise RuntimeError("Transform/Load stage failed")

            wrote_data = (
                PipelineStage.LOAD in self.result.stages
                and not skip_load
                and not self.config.dry_run
            )
            if wrote_data and refresh_readiness:
                self._refresh_readiness_once()

            if validate_contract and wrote_data:
                self._validate_completeness_contract(plan=plan, strict=strict)

            all_success = all(r.success for r in self.result.stages.values())
            self.result.status = PipelineStatus.COMPLETED if all_success else PipelineStatus.PARTIAL

            # Clean up uncompressed extracted files after successful load to save disk space
            if all_success and cleanup_extracted and wrote_data:
                self.logger.info("Cleaning up uncompressed extracted files after successful load")
                self.extract_manager.cleanup(sources=sources)
        except Exception as e:
            self.result.status = PipelineStatus.FAILED
            self.result.errors.append(str(e))
            self.logger.exception("Pipeline execution failed")

        finally:
            self.result.completed_at = datetime.now()
            self._run_completion_callbacks(self.result)

            self.logger.info(
                f"Pipeline {self.result.status.value}: " f"{self.result.duration:.1f}s total"
            )

        return self.result

    @staticmethod
    def _resolve_import_plan(
        *,
        plan: HarrisImportPlan | None,
        scope: str | None,
    ) -> HarrisImportPlan:
        """Resolve the modern import-plan interface and legacy scope adapter."""
        if plan is None:
            return HarrisImportPlan.from_legacy_scope(scope or "full")
        if scope is not None and plan != HarrisImportPlan.from_legacy_scope(scope):
            raise ValueError(
                "Pass either a Harris import plan or a matching legacy scope, not both"
            )
        return plan

    def _refresh_readiness_once(self) -> None:
        """Refresh property readiness exactly once per successful load run."""
        from .readiness import refresh_property_readiness

        self.logger.info("Refreshing property readiness once after load stage")
        refresh_property_readiness()

    def _validate_completeness_contract(self, plan: HarrisImportPlan, strict: bool) -> None:
        """Run validate_data with scope-aware skip flags."""

        try:
            call_command(
                "validate_data",
                **plan.contract_validation_options(),
            )
        except DjangoCommandError as exc:
            msg = f"Completeness validation failed: {exc}"
            if strict:
                raise RuntimeError(msg) from exc
            self.result.warnings.append(msg)
            self.logger.warning(msg)

    def _execute_download(self, sources: list[DataSource]) -> StageResult:
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
                "sources_total": len(sources),
                "sources_success": success_count,
                "bytes_downloaded": total_bytes,
            }

            if success_count < len(sources):
                failed = [r.source.name for r in results if not r.success]
                stage_result.error = f"Failed downloads: {', '.join(failed)}"

                # Only fail if required sources failed
                required_failed = [r for r in results if not r.success and r.source.required]
                stage_result.success = len(required_failed) == 0

        stage_result.completed_at = datetime.now()
        self._run_stage_callbacks(PipelineStage.DOWNLOAD, stage_result)
        return stage_result

    def _execute_extract(self, sources: list[DataSource]) -> StageResult:
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
                "archives_total": len(sources),
                "archives_success": success_count,
                "files_extracted": total_files,
                "bytes_extracted": total_bytes,
            }

            if success_count < len(sources):
                failed = [r.source.name for r in results if not r.success]
                stage_result.error = f"Failed extractions: {', '.join(failed)}"

                required_failed = [r for r in results if not r.success and r.source.required]
                stage_result.success = len(required_failed) == 0

        stage_result.completed_at = datetime.now()
        self._run_stage_callbacks(PipelineStage.EXTRACT, stage_result)
        return stage_result

    def _preload_fixtures(self, sources: list[DataSource]) -> None:
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
            if DEFAULT_HCAD_SOURCE_CATALOG.is_source(source, HcadSourceId.REAL_BUILDING_LAND):
                extract_path = self.extract_manager.get_extract_path(source)
                fixtures_path = extract_path / "fixtures.txt"

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
        sources: list[DataSource],
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
            total_invalid = 0
            total_skipped = 0
            total_failed = 0
            gis_loaded = 0
            source_errors: list[str] = []

            # STEP 1: Pre-load fixtures for bedroom/bathroom data
            # This must happen before processing building_res.txt
            self._preload_fixtures(sources)

            # Process each source type
            for source in sources:
                if source.source_type == DataSourceType.GIS_DATA:
                    # GIS data requires special handling
                    gis_result = self._process_gis_source(source)
                    gis_loaded += gis_result.get("loaded", 0)
                    total_invalid += gis_result.get("invalid", 0)
                    total_skipped += gis_result.get("skipped", 0)
                    total_failed += gis_result.get("failed", 0)
                    if gis_result.get("source_error"):
                        source_errors.append(str(gis_result["source_error"]))
                    continue

                # Find extracted files for this source
                extract_path = self.extract_manager.get_extract_path(source)
                if not extract_path.exists():
                    msg = f"Extract path not found for {source.name}: {extract_path}"
                    if source.required:
                        source_errors.append(msg)
                        self.logger.error(msg)
                    else:
                        self.result.warnings.append(msg)
                        self.logger.warning(msg)
                    continue

                # Process each data file in deterministic order.
                # Track per-schema truncate behavior so multi-file schemas like
                # extra_features_detail*.txt append after first load.
                schema_loaded: dict[str, bool] = {}
                data_files = sorted(extract_path.rglob("*.txt"))

                missing_required_files = self._missing_required_files(source, data_files)
                if missing_required_files:
                    msg = (
                        f"Required files missing for {source.name}: "
                        f"{', '.join(missing_required_files)}"
                    )
                    if source.required:
                        source_errors.append(msg)
                        self.logger.error(msg)
                    else:
                        self.result.warnings.append(msg)
                        self.logger.warning(msg)
                    continue

                # Prefer detailed extra feature files when present (legacy parity).
                if DEFAULT_HCAD_SOURCE_CATALOG.is_source(source, HcadSourceId.REAL_BUILDING_LAND):
                    has_extra_feature_details = any(
                        path.stem.lower().startswith("extra_features_detail") for path in data_files
                    )
                    if has_extra_feature_details:
                        data_files = [
                            path for path in data_files if path.stem.lower() != "extra_features"
                        ]

                for file_path in data_files:
                    schema_name = self._resolve_schema_name(file_path.stem)
                    if schema_name is None:
                        continue
                    truncate = not schema_loaded.get(schema_name, False)
                    result = self._process_data_file(file_path, skip_load, truncate=truncate)
                    schema_loaded[schema_name] = True
                    total_loaded += result.get("loaded", 0)
                    total_invalid += result.get("invalid", 0)
                    total_skipped += result.get("skipped", 0)
                    total_failed += result.get("failed", 0)

            metrics.records_processed = total_loaded + total_invalid + total_skipped + total_failed
            metrics.records_success = total_loaded
            metrics.records_failed = total_failed
            metrics.records_skipped = total_skipped

            stage_result.metrics = {
                "records_loaded": total_loaded,
                "records_invalid": total_invalid,
                "records_skipped": total_skipped,
                "records_failed": total_failed,
                "gis_coordinates_updated": gis_loaded,
            }

            if source_errors:
                stage_result.success = False
                stage_result.error = "; ".join(source_errors)
            elif total_failed > 0:
                stage_result.success = False
                stage_result.error = f"{total_failed} source processing failure(s)"

        stage_result.completed_at = datetime.now()
        self._run_stage_callbacks(PipelineStage.LOAD, stage_result)
        return stage_result

    def _iter_translated_rows(
        self,
        schema_name: str,
        file_path: Path,
    ) -> Iterator[RowResult]:
        """Return the shared translation stream for one supported source file."""
        if schema_name == "real_acct":
            return iter_property_rows(file_path)

        account_map = self.model_loader._get_account_to_property_map()
        if schema_name == "building_res":
            return iter_building_rows(
                file_path,
                account_map,
                self.model_loader.fixtures_aggregator,
            )
        if schema_name == "extra_features":
            return iter_extra_feature_rows(file_path, account_map)
        raise ValueError(f"Unsupported translated schema: {schema_name}")

    def _process_data_file(
        self,
        file_path: Path,
        skip_load: bool = False,
        truncate: bool = True,
    ) -> dict[str, int]:
        """Process a single data file.

        Args:
            file_path: Path to the data file
            skip_load: If True, only transform without loading to database
            truncate: If True, truncate the table before loading

        Returns:
            Dictionary with loaded/invalid/skipped/failed counts
        """
        # Determine schema based on filename
        filename = file_path.stem.lower()
        schema_name = self._resolve_schema_name(filename)

        if not schema_name:
            self.logger.debug(f"No schema for {file_path.name}, skipping")
            return {"loaded": 0, "invalid": 0, "skipped": 0, "failed": 0}

        self.logger.info(f"Processing {file_path.name} with schema {schema_name}")

        if skip_load:
            counts = {"loaded": 0, "invalid": 0, "skipped": 0, "failed": 0}
            for row in self._iter_translated_rows(schema_name, file_path):
                if row.skip:
                    counts["skipped"] += 1
                elif row.invalid:
                    counts["invalid"] += 1
                else:
                    counts["loaded"] += 1
            return counts

        rows = self._iter_translated_rows(schema_name, file_path)
        from .fast_loader import (
            copy_load_building_rows,
            copy_load_property_rows,
            postgres_backend,
        )

        if postgres_backend() and schema_name in ("real_acct", "building_res"):
            if schema_name == "real_acct":
                fast = copy_load_property_rows(rows, truncate=truncate)
                # PropertyRecord ids changed; rebuild the account caches that the
                # building/extra-feature loaders depend on.
                self.model_loader.reset_cache()
                return {
                    "loaded": fast["loaded"],
                    "invalid": 0,
                    "skipped": fast["skipped"],
                    "failed": 0,
                }
            fast = copy_load_building_rows(rows, truncate=truncate)
            return {
                "loaded": fast["loaded"],
                "invalid": fast["invalid"],
                "skipped": fast["skipped"],
                "failed": 0,
            }

        if schema_name == "real_acct":
            result = self.model_loader.load_property_records(rows, truncate=truncate)
        elif schema_name == "building_res":
            result = self.model_loader.load_building_details(rows, truncate=truncate)
        elif schema_name == "extra_features":
            result = self.model_loader.load_extra_features(rows, truncate=truncate)
        else:
            return {"loaded": 0, "invalid": 0, "skipped": 0, "failed": 0}

        return {
            "loaded": result.records_loaded,
            "invalid": result.records_invalid,
            "skipped": result.records_skipped,
            "failed": 1 if result.error else 0,
        }

    @staticmethod
    def _resolve_schema_name(filename_stem: str) -> str | None:
        """Map a source filename stem to a transform schema name."""
        filename = filename_stem.lower()

        # Skip code description files (lookup tables, not actual data)
        if filename.startswith("desc_"):
            return None

        if filename == "real_acct":
            return "real_acct"
        if filename == "building_res":
            return "building_res"
        if filename == "extra_features" or filename.startswith("extra_features_detail"):
            return "extra_features"
        return None

    def _process_gis_source(self, source: DataSource) -> dict[str, Any]:
        """Process GIS data source.

        Finds shapefiles in the extracted GIS data and loads coordinates
        into PropertyRecord latitude/longitude fields.

        Args:
            source: The GIS data source configuration

        Returns:
            Dictionary with loaded/invalid/skipped/failed counts.
        """
        self.logger.info(f"Processing GIS source: {source.name}")

        # Get the extract path for GIS data
        extract_path = self.extract_manager.get_extract_path(source)
        if not extract_path.exists():
            msg = f"GIS extract path not found: {extract_path}"
            self.logger.error(msg)
            return {"loaded": 0, "invalid": 0, "skipped": 0, "failed": 1, "source_error": msg}

        # Legacy extracts often lived under the download tree. Include that
        # location as a fallback so modern and legacy commands pick equivalent data.
        archive_base = Path(source.filename).name.rsplit(".", 1)[0]
        legacy_extract_path = self.config.download_dir / archive_base

        candidate_roots = [extract_path]
        if legacy_extract_path.exists() and legacy_extract_path != extract_path:
            candidate_roots.append(legacy_extract_path)

        shapefile_path = self._select_preferred_gis_shapefile(candidate_roots)
        if shapefile_path is None:
            searched = ", ".join(str(root) for root in candidate_roots)
            msg = f"No shapefiles found in candidate roots: {searched}"
            self.logger.error(msg)
            return {"loaded": 0, "invalid": 0, "skipped": 0, "failed": 1, "source_error": msg}

        self.logger.info(f"Loading GIS data from: {shapefile_path}")

        try:
            from .gis_loader import load_gis_parcels

            count = load_gis_parcels(str(shapefile_path), refresh_readiness=False)
            self.logger.info(f"Updated {count} properties with GIS coordinates")
            return {"loaded": count, "invalid": 0, "skipped": 0, "failed": 0}

        except ImportError as e:
            self.logger.error(f"GIS processing requires geopandas: {e}")
            return {"loaded": 0, "invalid": 0, "skipped": 0, "failed": 1, "source_error": str(e)}
        except Exception as e:
            self.logger.exception(f"Error processing GIS data: {e}")
            return {"loaded": 0, "invalid": 0, "skipped": 0, "failed": 1, "source_error": str(e)}

    @staticmethod
    def _missing_required_files(source: DataSource, data_files: list[Path]) -> list[str]:
        """Return missing required files for core required property sources."""
        return DEFAULT_HCAD_SOURCE_CATALOG.missing_required_files(
            source,
            [str(path) for path in data_files],
        )

    @staticmethod
    def _select_preferred_gis_shapefile(search_roots: list[Path]) -> Path | None:
        """Compatibility adapter for the shared GIS selection rule."""
        from .gis_loader import select_preferred_gis_shapefile

        return select_preferred_gis_shapefile(search_roots)

    def execute_download_only(
        self,
        sources: list[DataSource] | None = None,
        include_optional: bool = False,
    ) -> list[DownloadResult]:
        """Execute only the download stage."""
        if sources is None:
            if include_optional:
                sources = self.config.get_all_sources()
            else:
                sources = self.config.get_required_sources()

        return self.download_manager.download_batch(sources)

    def execute_extract_only(
        self,
        sources: list[DataSource] | None = None,
    ) -> list[ExtractResult]:
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

    def get_status(self) -> dict[str, Any]:
        """Get current pipeline status."""
        return {
            "current_stage": self.current_stage.value if self.current_stage else None,
            "result": self.result.to_dict() if self.result else None,
            "config": {
                "data_year": self.config.data_year,
                "dry_run": self.config.dry_run,
                "continue_on_error": self.config.continue_on_error,
            },
        }
