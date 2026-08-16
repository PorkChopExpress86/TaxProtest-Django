"""
ETL Pipeline Orchestrator

Coordinates all ETL stages with strict validation, robust error handling,
and metrics collection.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from django.core.management import call_command
from django.core.management.base import CommandError as DjangoCommandError

from .config import DataSource, DataSourceType, ETLConfig
from .download import DownloadManager, DownloadResult
from .extract import ExtractManager, ExtractResult
from .logging import ETLLogger
from .model_loader import ModelLoader
from .row_reader import (
    iter_building_rows,
    iter_extra_feature_rows,
    iter_property_rows,
)


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
        skip_download: bool | None = None,
        skip_extract: bool | None = None,
        skip_transform: bool | None = None,
        skip_load: bool | None = None,
        cleanup_extracted: bool | None = None,
        scope: str = "full",
        strict: bool = True,
        validate_contract: bool = True,
    ) -> PipelineResult:
        """Execute the full ETL pipeline.

        Args:
            sources: Specific sources to process (default: all required)
            skip_download: Skip download stage
            skip_extract: Skip extract stage
            skip_transform: Skip transform stage
            skip_load: Skip load stage
            cleanup_extracted: Delete uncompressed extracted files after successful load
            scope: Pipeline scope (full, building-only, gis-only, property-only)
            strict: Fail run on required gaps/errors
            validate_contract: Run post-load validate_data checks

        Returns:
            PipelineResult with execution status and metrics
        """
        if scope not in {"full", "building-only", "gis-only", "property-only"}:
            raise ValueError(f"Unsupported pipeline scope: {scope}")

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

        sources = self._select_sources_for_scope(scope, sources)

        self.logger.info(
            f"Starting ETL pipeline for {len(sources)} sources "
            f"(year={self.config.data_year}, scope={scope}, strict={strict}, dry_run={self.config.dry_run})"
        )

        try:
            # Download + Extract stages (pipelined per-source for I/O overlap).
            # Each source downloads then extracts as a unit, with sources
            # running concurrently so the big real_acct download overlaps with
            # the building_land extract, etc.
            if not skip_download or not skip_extract:
                download_result, extract_result = self._execute_download_extract(
                    sources, skip_download=skip_download, skip_extract=skip_extract
                )
                self.result.stages[PipelineStage.DOWNLOAD] = download_result
                self.result.stages[PipelineStage.EXTRACT] = extract_result

                if not download_result.success and strict:
                    raise RuntimeError("Download stage failed")
                if not extract_result.success and strict:
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
            if wrote_data:
                self._refresh_readiness_once()

            if validate_contract and wrote_data:
                self._validate_completeness_contract(scope=scope, strict=strict)

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

    def _select_sources_for_scope(
        self,
        scope: str,
        sources: list[DataSource] | None,
    ) -> list[DataSource]:
        """Return the source list for a pipeline scope."""
        selected = list(sources or self.config.get_required_sources())
        if scope == "full":
            return selected
        if scope == "building-only":
            return [s for s in selected if s.name == "Real Building Land"]
        if scope == "gis-only":
            return [s for s in selected if s.source_type == DataSourceType.GIS_DATA]
        if scope == "property-only":
            return [s for s in selected if s.source_type == DataSourceType.PROPERTY_DATA]
        return selected

    def _refresh_readiness_once(self) -> None:
        """Refresh property readiness exactly once per successful load run."""
        from .readiness import refresh_property_readiness

        self.logger.info("Refreshing property readiness once after load stage")
        refresh_property_readiness()

    def _validate_completeness_contract(self, scope: str, strict: bool) -> None:
        """Run validate_data with scope-aware skip flags."""
        skip_building_checks = scope in {"gis-only", "property-only"}
        skip_gis_checks = scope in {"building-only", "property-only"}

        try:
            call_command(
                "validate_data",
                skip_building_checks=skip_building_checks,
                skip_gis_checks=skip_gis_checks,
            )
        except DjangoCommandError as exc:
            msg = f"Completeness validation failed: {exc}"
            if strict:
                raise RuntimeError(msg) from exc
            self.result.warnings.append(msg)
            self.logger.warning(msg)

    def _execute_download_extract(
        self,
        sources: list[DataSource],
        *,
        skip_download: bool = False,
        skip_extract: bool = False,
    ) -> tuple[StageResult, StageResult]:
        """Execute download and extract stages as a per-source pipeline.

        Each source downloads then extracts as a unit. Sources run concurrently
        via a thread pool, so the download of one source overlaps with the
        extract of another — the main cold-run speedup, since the 224MB
        building_land download no longer blocks extraction of the already-
        finished 201MB real_acct archive.
        """
        download_result = StageResult(stage=PipelineStage.DOWNLOAD, success=True)
        extract_result = StageResult(stage=PipelineStage.EXTRACT, success=True)
        self.current_stage = PipelineStage.DOWNLOAD

        self.logger.info(
            f"Download+Extract pipeline: {len(sources)} sources "
            f"(skip_download={skip_download}, skip_extract={skip_extract})"
        )

        download_results: list[DownloadResult] = []
        extract_results: list[ExtractResult] = []

        with self.logger.stage("download_extract") as metrics:
            max_parallel = self.download_manager.download_config.max_parallel

            def _process_source(source: DataSource) -> tuple[DownloadResult, ExtractResult]:
                """Download then extract a single source."""
                dl_result = DownloadResult(source=source, success=True, bytes_downloaded=0)
                if not skip_download:
                    dl_result = self.download_manager.download_file(source)

                if skip_extract:
                    ex_result = ExtractResult(source=source, success=True, files_extracted=[])
                elif not dl_result.success:
                    # Never reached the archive, so this is not a clean extract.
                    # Reporting success here would hide the source in the
                    # extract metrics while only the download stage flagged it.
                    ex_result = ExtractResult(
                        source=source,
                        success=False,
                        files_extracted=[],
                        error=f"Skipped: download failed ({dl_result.error or 'unknown error'})",
                    )
                else:
                    ex_result = self.extract_manager.extract_archive(source)

                return dl_result, ex_result

            # Sort by priority (lowest = highest priority = starts first).
            ordered = sorted(sources, key=lambda s: s.priority)

            with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                future_to_source = {
                    executor.submit(_process_source, source): source for source in ordered
                }

                for future in as_completed(future_to_source):
                    source = future_to_source[future]
                    try:
                        dl, ex = future.result()
                        download_results.append(dl)
                        extract_results.append(ex)
                    except Exception as e:
                        self.logger.error(f"Source {source.name} failed: {e}")
                        download_results.append(
                            DownloadResult(source=source, success=False, error=str(e))
                        )
                        extract_results.append(
                            ExtractResult(source=source, success=False, error=str(e))
                        )

            # Aggregate download metrics
            dl_success = sum(1 for r in download_results if r.success)
            dl_bytes = sum(r.bytes_downloaded for r in download_results)
            metrics.records_processed = len(download_results)
            metrics.bytes_downloaded = dl_bytes

            download_result.metrics = {
                "sources_total": len(sources),
                "sources_success": dl_success,
                "bytes_downloaded": dl_bytes,
            }
            if dl_success < len(sources):
                failed = [r.source.name for r in download_results if not r.success]
                download_result.error = f"Failed downloads: {', '.join(failed)}"
                required_failed = [
                    r for r in download_results if not r.success and r.source.required
                ]
                download_result.success = len(required_failed) == 0

            # Aggregate extract metrics
            ex_success = sum(1 for r in extract_results if r.success)
            ex_bytes = sum(r.bytes_extracted for r in extract_results)
            ex_files = sum(len(r.files_extracted or []) for r in extract_results)
            metrics.bytes_extracted = ex_bytes
            # One shared metrics record covers both halves of this stage, so
            # count a source as a success only when it downloaded *and*
            # extracted — assigning each half in turn just left the last one.
            metrics.records_success = sum(
                1 for dl, ex in zip(download_results, extract_results) if dl.success and ex.success
            )
            metrics.records_failed = len(download_results) - metrics.records_success

            extract_result.metrics = {
                "archives_total": len(sources),
                "archives_success": ex_success,
                "files_extracted": ex_files,
                "bytes_extracted": ex_bytes,
            }
            if ex_success < len(sources):
                failed = [r.source.name for r in extract_results if not r.success]
                extract_result.error = f"Failed extractions: {', '.join(failed)}"
                required_failed = [
                    r for r in extract_results if not r.success and r.source.required
                ]
                extract_result.success = len(required_failed) == 0

        download_result.completed_at = datetime.now()
        extract_result.completed_at = datetime.now()
        self._run_stage_callbacks(PipelineStage.DOWNLOAD, download_result)
        self._run_stage_callbacks(PipelineStage.EXTRACT, extract_result)
        return download_result, extract_result

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
            if source.name == "Real Building Land":
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
        from the row reader directly to load without buffering.
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
                if source.name == "Real Building Land":
                    has_extra_feature_details = any(
                        path.stem.lower().startswith("extra_features_detail") for path in data_files
                    )
                    if has_extra_feature_details:
                        data_files = [
                            path for path in data_files if path.stem.lower() != "extra_features"
                        ]

                for file_path in data_files:
                    kind = self._resolve_file_kind(file_path.stem)
                    if kind is None:
                        continue
                    truncate = not schema_loaded.get(kind, False)
                    result = self._process_data_file(file_path, skip_load, truncate=truncate)
                    schema_loaded[kind] = True
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

    def _process_data_file(
        self,
        file_path: Path,
        skip_load: bool = False,
        truncate: bool = True,
    ) -> dict[str, int]:
        """Process a single data file.

        Args:
            file_path: Path to the data file
            skip_load: If True, only count rows without loading to database
            truncate: If True, truncate the table before loading

        Returns:
            Dictionary with loaded/invalid/skipped/failed counts
        """
        kind = self._resolve_file_kind(file_path.stem)
        if kind is None:
            self.logger.debug(f"No loader for {file_path.name}, skipping")
            return {"loaded": 0, "invalid": 0, "skipped": 0, "failed": 0}

        self.logger.info(f"Processing {file_path.name} as {kind}")

        rows = self._iter_rows(kind, file_path)

        if skip_load:
            # Stream count-only without materializing all rows.
            loaded = 0
            for result in rows:
                if result.row is not None:
                    loaded += 1
            return {"loaded": loaded, "invalid": 0, "skipped": 0, "failed": 0}

        # Fast path: real_acct/building_res stream straight into PostgreSQL via
        # COPY, skipping the row-by-row bulk_create path.
        from .fast_loader import (
            copy_load_building_details,
            copy_load_property_records,
            postgres_backend,
        )

        if postgres_backend() and kind in ("real_acct", "building_res"):
            if kind == "real_acct":
                fast = copy_load_property_records(rows, truncate=truncate)
                # PropertyRecord ids changed; rebuild the account caches that the
                # building/extra-feature loaders depend on.
                self.model_loader.reset_cache()
                return {
                    "loaded": fast["loaded"],
                    "invalid": 0,
                    "skipped": fast["skipped"],
                    "failed": 0,
                }
            fast = copy_load_building_details(rows, truncate=truncate)
            return {
                "loaded": fast["loaded"],
                "invalid": fast["invalid"],
                "skipped": fast["skipped"],
                "failed": 0,
            }

        # ORM path: load typed rows into Django models.
        if kind == "real_acct":
            result = self.model_loader.load_property_records(rows, truncate=truncate)
        elif kind == "building_res":
            result = self.model_loader.load_building_details(rows, truncate=truncate)
        elif kind == "extra_features":
            result = self.model_loader.load_extra_features(rows, truncate=truncate)
        else:
            return {"loaded": 0, "invalid": 0, "skipped": 0, "failed": 0}

        return {
            "loaded": result.records_loaded,
            "invalid": result.records_invalid,
            "skipped": result.records_skipped,
            "failed": 1 if result.error else 0,
        }

    def _iter_rows(self, kind: str, file_path: Path):
        """Yield RowResults for a data file, wiring the account map and fixtures."""
        if kind == "real_acct":
            return iter_property_rows(file_path)
        if kind == "building_res":
            return iter_building_rows(
                file_path,
                account_map=self.model_loader._get_account_to_property_map(),
                fixtures_aggregator=self.model_loader.fixtures_aggregator,
            )
        if kind == "extra_features":
            return iter_extra_feature_rows(
                file_path,
                account_map=self.model_loader._get_account_to_property_map(),
            )
        return iter(())

    @staticmethod
    def _resolve_file_kind(filename_stem: str) -> str | None:
        """Map a source filename stem to a row-reader kind."""
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
        into the ParcelGeometry table. This is a bulk INSERT/upsert, not an
        UPDATE of PropertyRecord, so it can run at any point in the pipeline
        — independent of whether properties have been loaded yet.

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
            # Import and call the GIS loading function
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
        stems = {path.stem.lower() for path in data_files}
        missing: list[str] = []

        if source.name == "Real Account Owner":
            if "real_acct" not in stems:
                missing.append("real_acct.txt")

        if source.name == "Real Building Land":
            if "building_res" not in stems:
                missing.append("building_res.txt")
            if "fixtures" not in stems:
                missing.append("fixtures.txt")

            has_extra_features = "extra_features" in stems or any(
                stem.startswith("extra_features_detail") for stem in stems
            )
            if not has_extra_features:
                missing.append("extra_features*.txt")

        return missing

    @staticmethod
    def _select_preferred_gis_shapefile(search_roots: list[Path]) -> Path | None:
        """Select the best shapefile candidate from one or more roots.

        Preference order matches the legacy GIS loader behavior:
        1) Exact `ParcelsCity.shp`
        2) Any shapefile containing `ParcelsCity`
        3) Paths under `/Gis/pdata/`
        4) Shorter path depth as stable tie-breaker
        """
        shapefiles: list[Path] = []
        seen: set[str] = set()

        for root in search_roots:
            if not root.exists():
                continue
            for path in root.rglob("*.shp"):
                normalized = str(path).replace("\\", "/")
                if normalized in seen:
                    continue
                seen.add(normalized)
                shapefiles.append(path)

        if not shapefiles:
            gdb_dirs: list[Path] = []
            for root in search_roots:
                if not root.exists():
                    continue
                for path in root.rglob("*.gdb"):
                    if path.is_dir():
                        normalized = str(path).replace("\\", "/")
                        if normalized not in seen:
                            seen.add(normalized)
                            gdb_dirs.append(path)

            if gdb_dirs:
                return min(
                    gdb_dirs,
                    key=lambda p: (0 if "parcels.gdb" in p.name.lower() else 1, len(p.parts)),
                )
            return None

        def priority(path: Path) -> tuple[int, int, int]:
            normalized = str(path).replace("\\", "/").lower()
            name = path.name.lower()
            return (
                2 if name == "parcelscity.shp" else 1 if "parcelscity" in name else 0,
                1 if "/gis/pdata/" in normalized else 0,
                -len(path.parts),
            )

        return max(shapefiles, key=priority)

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
