"""Deep Harris import boundary and its private per-call implementation."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

from django.core.management import call_command
from django.core.management.base import CommandError as DjangoCommandError
from django.db import DatabaseError
from django.utils import timezone

from counties.common.import_logging import import_warnings
from counties.common.import_recovery import ReplayRequest, requested_replay, verify_replay
from counties.common.import_writers import county_writer, fenced_write, working_source_root
from counties.common.models import ImportCandidate, ImportOperation
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG

from .config import DataSource, DataSourceType, ETLConfig
from .download import DownloadManager
from .extract import ExtractManager
from .fixtures_aggregator import FixturesAggregator
from .import_plan import HarrisImportPlan
from .logging import ETLLogger
from .source_pipeline import HarrisSourcePipeline, resolve_schema_name


class HarrisImportPhase(Enum):
    """Stable serialized phases in a Harris import result."""

    DOWNLOAD = "download"
    EXTRACT = "extract"
    SOURCE_VALIDATION = "source_validation"
    LOAD = "load"


class HarrisImportStatus(Enum):
    """Overall Harris import status."""

    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    PREPARED = "prepared"
    AWAITING_REVIEW = "awaiting_review"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class HarrisImportStageResult:
    """Result for one completed Harris import phase."""

    stage: HarrisImportPhase
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
class _StageResult:
    """Mutable phase builder that never crosses the import boundary."""

    stage: HarrisImportPhase
    success: bool
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None
    error: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def freeze(self) -> HarrisImportStageResult:
        return HarrisImportStageResult(
            stage=self.stage,
            success=self.success,
            started_at=self.started_at,
            completed_at=self.completed_at,
            error=self.error,
            metrics=MappingProxyType(
                {key: value for key, value in self.metrics.items() if not key.startswith("_")}
            ),
        )


@dataclass(frozen=True)
class HarrisImportResult:
    """Immutable result returned by the Harris import boundary."""

    status: HarrisImportStatus
    started_at: datetime
    completed_at: datetime | None = None
    stages: Mapping[HarrisImportPhase, HarrisImportStageResult] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    wrote_data: bool = False
    already_applied: bool = False
    operation_id: UUID | None = None
    candidate_id: UUID | None = None

    @property
    def duration(self) -> float:
        if self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return 0.0

    @property
    def success(self) -> bool:
        return self.status == HarrisImportStatus.COMPLETED

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
                    "metrics": {
                        key: value
                        for key, value in result.metrics.items()
                        if not key.startswith("_")
                    },
                }
                for stage, result in self.stages.items()
            },
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


class HarrisAcquisitionMode(StrEnum):
    FETCH = "fetch"
    REUSE_DOWNLOADED = "reuse_downloaded"


class HarrisExtractionMode(StrEnum):
    EXTRACT = "extract"
    REUSE_EXTRACTED = "reuse_extracted"


class HarrisFailurePolicy(StrEnum):
    STRICT = "strict"
    BEST_EFFORT = "best_effort"


class ExtractedSourceRetention(StrEnum):
    REMOVE_AFTER_SUCCESS = "remove_after_success"
    RETAIN = "retain"


@dataclass(frozen=True, slots=True)
class HarrisPreview:
    """Translate selected sources without changing persisted Harris data."""


@dataclass(frozen=True, slots=True)
class HarrisApply:
    """Apply translated rows and own all post-write obligations."""

    refresh_readiness: bool = True
    validate_completeness: bool = True
    extracted_source_retention: ExtractedSourceRetention = (
        ExtractedSourceRetention.REMOVE_AFTER_SUCCESS
    )

    def __post_init__(self) -> None:
        if not isinstance(self.refresh_readiness, bool):
            raise InvalidHarrisImportRequest("refresh_readiness must be a boolean")
        if not isinstance(self.validate_completeness, bool):
            raise InvalidHarrisImportRequest("validate_completeness must be a boolean")
        if not isinstance(self.extracted_source_retention, ExtractedSourceRetention):
            raise InvalidHarrisImportRequest(
                "extracted_source_retention must be an ExtractedSourceRetention"
            )


@dataclass(frozen=True, slots=True)
class HarrisPrepare(HarrisApply):
    """Persist an isolated candidate for inspection without publication."""


HarrisLoadIntent = HarrisPreview | HarrisApply | HarrisPrepare


@dataclass(frozen=True)
class HarrisPropertyFile:
    path: Path
    append: bool = False
    limit: int | None = None
    batch_size: int = 5000


@dataclass(frozen=True, kw_only=True, slots=True)
class HarrisImportRequest:
    """One complete, validated request to import catalog-selected HCAD data."""

    plan: HarrisImportPlan
    data_year: int | None = None
    acquisition: HarrisAcquisitionMode = HarrisAcquisitionMode.FETCH
    extraction: HarrisExtractionMode = HarrisExtractionMode.EXTRACT
    load: HarrisLoadIntent = field(default_factory=HarrisApply)
    failure_policy: HarrisFailurePolicy = HarrisFailurePolicy.STRICT
    actor: str = ""
    origin: str = "operator"
    candidate_id: UUID | None = None
    property_file: HarrisPropertyFile | None = None
    application_reason: str = ""
    replay: ReplayRequest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.plan, HarrisImportPlan):
            raise InvalidHarrisImportRequest("plan must be a HarrisImportPlan")
        if self.replay is not None and (
            self.candidate_id is not None or not isinstance(self.load, HarrisPrepare)
        ):
            raise InvalidHarrisImportRequest("Dataset recovery requires fresh HarrisPrepare intent")
        if self.candidate_id is not None and (
            not isinstance(self.load, HarrisApply) or isinstance(self.load, HarrisPrepare)
        ):
            raise InvalidHarrisImportRequest(
                "Candidate application requires explicit HarrisApply intent"
            )
        if self.data_year is not None and (
            not isinstance(self.data_year, int) or isinstance(self.data_year, bool)
        ):
            raise InvalidHarrisImportRequest("Harris import data year must be an integer")
        if self.data_year is not None and self.data_year < 2000:
            raise InvalidHarrisImportRequest("Harris import data year must be 2000 or later")
        if not isinstance(self.acquisition, HarrisAcquisitionMode):
            raise InvalidHarrisImportRequest("acquisition must be a HarrisAcquisitionMode")
        if not isinstance(self.extraction, HarrisExtractionMode):
            raise InvalidHarrisImportRequest("extraction must be a HarrisExtractionMode")
        if not isinstance(self.load, (HarrisPreview, HarrisApply)):
            raise InvalidHarrisImportRequest("load must be HarrisPreview or HarrisApply")
        if not isinstance(self.failure_policy, HarrisFailurePolicy):
            raise InvalidHarrisImportRequest("failure_policy must be a HarrisFailurePolicy")


class InvalidHarrisImportRequest(ValueError):
    """Raised before side effects when an import request is invalid."""


@dataclass(frozen=True, slots=True)
class HarrisImportEvent:
    """An observational progress event emitted by a Harris import."""

    phase: HarrisImportPhase
    message: str


class HarrisImportReporter(Protocol):
    """Observe import progress without controlling execution."""

    def report(self, event: HarrisImportEvent) -> None: ...


class _HarrisImportExecution:
    """Private state for one call to :func:`run_harris_import`.

    Features:
    - Stage-by-stage execution
    - Dependency management
    - Error handling and recovery
    - Comprehensive logging
    - Metrics collection
    A new instance is created for every request, so no execution state or
    progress hooks survive into a later import.
    """

    def __init__(
        self,
        request: HarrisImportRequest,
        sources: list[DataSource],
        data_year: int,
        config: ETLConfig | None = None,
        logger: ETLLogger | None = None,
        reporter: HarrisImportReporter | None = None,
        operation: ImportOperation | None = None,
    ):
        self.request = request
        self.operation = operation
        self.sources = sources
        self.data_year = data_year
        self.config = config or ETLConfig.from_env()
        self.config.download_dir = working_source_root(
            self.config.download_dir, reuse=request.replay is None
        )
        self.config.extract_dir = working_source_root(
            self.config.extract_dir, reuse=request.replay is None
        )
        if request.replay is not None:
            from .recovery import seed_sources

            seed_sources(self.config, sources, operation)
        if request.property_file is not None:
            directory = self.config.extract_dir / "Real_acct_owner"
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(request.property_file.path, directory / "real_acct.txt")
        self.logger = logger or ETLLogger(
            name="etl_orchestrator",
            log_dir=self.config.log_dir,
            log_level=self.config.logging.level,
            operation_id=str(operation.pk) if operation else None,
        )

        # Initialize managers
        self.download_manager = DownloadManager(
            self.config,
            self.logger,
            data_year=data_year,
        )
        self.extract_manager = ExtractManager(self.config, self.logger)
        self.fixtures_aggregator = FixturesAggregator()

        self.reporter = reporter
        self.stages: dict[HarrisImportPhase, HarrisImportStageResult] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def run(self) -> HarrisImportResult:
        """Execute the request while keeping ordering and policy inside this module."""
        started_at = datetime.now()
        plan = self.request.plan
        sources = self.sources

        preview = isinstance(self.request.load, HarrisPreview)
        strict = self.request.failure_policy is HarrisFailurePolicy.STRICT

        self.logger.info(
            f"Starting ETL pipeline for {len(sources)} sources "
            f"(year={self.data_year}, plan={plan.legacy_scope}, "
            f"strict={strict}, preview={preview})"
        )

        if self.request.acquisition is HarrisAcquisitionMode.FETCH:
            with fenced_write():
                download_result = self._execute_download(sources)
            if not self._record_stage(download_result, strict=strict):
                return self._finish(started_at, HarrisImportStatus.FAILED, wrote_data=False)

        if self.request.extraction is HarrisExtractionMode.EXTRACT:
            with fenced_write():
                extract_result = self._execute_extract(sources)
            if not self._record_stage(extract_result, strict=strict):
                return self._finish(started_at, HarrisImportStatus.FAILED, wrote_data=False)

        if preview or isinstance(self.request.load, HarrisPrepare):
            from .source_validation import validate_harris_sources

            assert self.operation is not None
            validation = validate_harris_sources(
                self.config, sources, self.data_year, self.operation
            )
            self._account_to_property = validation.account_map
            source_result = _StageResult(
                stage=HarrisImportPhase.SOURCE_VALIDATION,
                success=not validation.errors,
                error="; ".join(validation.errors) or None,
                metrics=validation.evidence(),
                completed_at=datetime.now(),
            )
            if not self._record_stage(source_result, strict=True):
                return self._finish(started_at, HarrisImportStatus.FAILED, wrote_data=False)

        if preview:
            load_result = _StageResult(
                stage=HarrisImportPhase.LOAD,
                success=True,
                metrics=validation.metrics(),
                completed_at=datetime.now(),
            )
        else:
            with fenced_write():
                load_result = self._execute_transform_load(sources, strict=strict)
        wrote_data = bool(load_result.metrics.get("_wrote_data", False))
        useful_work = bool(load_result.metrics.get("_sources_succeeded", 0))
        if not self._record_stage(load_result, strict=strict):
            return self._finish(started_at, HarrisImportStatus.FAILED, wrote_data=wrote_data)

        if wrote_data:
            apply = self.request.load
            assert isinstance(apply, HarrisApply)
            if apply.refresh_readiness:
                try:
                    self._refresh_readiness_once()
                except DatabaseError as exc:
                    if not self._record_expected_failure(
                        f"Readiness refresh failed: {exc}", strict=strict
                    ):
                        return self._finish(started_at, HarrisImportStatus.FAILED, wrote_data=True)

            if apply.validate_completeness:
                if not self._validate_completeness_contract(plan=plan, strict=strict):
                    return self._finish(started_at, HarrisImportStatus.FAILED, wrote_data=True)

        all_success = all(result.success for result in self.stages.values()) and not self.errors
        if all_success:
            status = HarrisImportStatus.COMPLETED
        elif useful_work:
            status = HarrisImportStatus.PARTIAL
        else:
            status = HarrisImportStatus.FAILED

        if (
            status is HarrisImportStatus.COMPLETED
            and wrote_data
            and isinstance(self.request.load, HarrisApply)
            and self.request.load.extracted_source_retention
            is ExtractedSourceRetention.REMOVE_AFTER_SUCCESS
        ):
            self.logger.info("Cleaning up selected extracted files after completed import")
            try:
                with fenced_write():
                    self.extract_manager.cleanup(sources=sources)
            except OSError as exc:
                self.warnings.append(
                    f"Import committed, but extracted-source cleanup failed: {exc}"
                )

        return self._finish(started_at, status, wrote_data=wrote_data)

    def _record_stage(self, result: _StageResult, *, strict: bool) -> bool:
        frozen = result.freeze()
        self.stages[result.stage] = frozen
        self._report(result.stage, f"{result.stage.value} stage")
        if result.success:
            return True
        message = result.error or f"{result.stage.value} stage failed"
        self.errors.append(message)
        return not strict

    def _record_expected_failure(self, message: str, *, strict: bool) -> bool:
        self.errors.append(message)
        return not strict

    def _report(self, phase: HarrisImportPhase, message: str) -> None:
        if self.reporter is None:
            return
        try:
            self.reporter.report(HarrisImportEvent(phase=phase, message=message))
        except Exception as exc:
            warning = f"Import progress reporter failed: {exc}"
            self.warnings.append(warning)
            self.logger.warning(warning)

    def _finish(
        self,
        started_at: datetime,
        status: HarrisImportStatus,
        *,
        wrote_data: bool,
    ) -> HarrisImportResult:
        result = HarrisImportResult(
            status=status,
            started_at=started_at,
            completed_at=datetime.now(),
            stages=MappingProxyType(dict(self.stages)),
            errors=tuple(self.errors),
            warnings=tuple(self.warnings),
            wrote_data=wrote_data,
        )
        self.logger.info(f"Pipeline {result.status.value}: {result.duration:.1f}s total")
        return result

    def _refresh_readiness_once(self) -> None:
        """Refresh property readiness exactly once per successful load run."""
        from .readiness import refresh_property_readiness

        self.logger.info("Refreshing property readiness once after load stage")
        with fenced_write():
            refresh_property_readiness()

    def _validate_completeness_contract(self, plan: HarrisImportPlan, strict: bool) -> bool:
        """Run validate_data with scope-aware skip flags."""

        try:
            call_command(
                "validate_data",
                **plan.contract_validation_options(),
            )
        except DjangoCommandError as exc:
            msg = f"Completeness validation failed: {exc}"
            if strict:
                self.errors.append(msg)
                return False
            self.errors.append(msg)
            self.logger.warning(msg)
        return True

    def _execute_download(self, sources: list[DataSource]) -> _StageResult:
        """Execute download stage."""
        stage_result = _StageResult(stage=HarrisImportPhase.DOWNLOAD, success=True)

        self.logger.info(f"Download stage: {len(sources)} sources")

        with self.logger.stage("download") as metrics:
            results = self.download_manager.download_batch(sources)
            if self.operation is not None:
                self.operation.evidence["acquired_sources"] = {
                    result.source.filename: {
                        "source_url": result.source_url,
                        "source_year": next(
                            (
                                candidate
                                for candidate in DEFAULT_HCAD_SOURCE_CATALOG.candidate_years(
                                    self.data_year
                                )
                                if "{year}" in result.source.url_template
                                and result.source.get_url(candidate) == result.source_url
                            ),
                            None,
                        ),
                    }
                    for result in results
                    if result.success
                }

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
        return stage_result

    def _execute_extract(self, sources: list[DataSource]) -> _StageResult:
        """Execute extract stage."""
        stage_result = _StageResult(stage=HarrisImportPhase.EXTRACT, success=True)

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
        return stage_result

    def _execute_transform_load(
        self,
        sources: list[DataSource],
        skip_load: bool = False,
        strict: bool = True,
    ) -> _StageResult:
        """Execute transform and load stages.

        These are combined for memory efficiency - records are streamed
        from transform directly to load without buffering.
        """
        stage_result = _StageResult(stage=HarrisImportPhase.LOAD, success=True)

        self.logger.info("Transform/Load stage")

        with self.logger.stage("transform_load") as metrics:
            total_loaded = 0
            total_invalid = 0
            total_skipped = 0
            total_failed = 0
            gis_loaded = 0
            source_errors: list[str] = []
            sources_succeeded = 0

            pipeline = HarrisSourcePipeline(
                extract_manager=self.extract_manager,
                fixtures_aggregator=self.fixtures_aggregator,
                property_file=self.request.property_file,
                logger=self.logger,
            )
            pipeline.preload_fixtures(sources)

            for source in sources:
                if source.source_type == DataSourceType.GIS_DATA:
                    gis_result = self._process_gis_source(source, preview=skip_load)
                    gis_loaded += gis_result.get("loaded", 0)
                    total_invalid += gis_result.get("invalid", 0)
                    total_skipped += gis_result.get("skipped", 0)
                    total_failed += gis_result.get("failed", 0)
                    if gis_result.get("source_error"):
                        source_errors.append(str(gis_result["source_error"]))
                        if strict:
                            break
                    else:
                        sources_succeeded += 1
                    continue

                source_result = pipeline.process_tabular_source(
                    source,
                    skip_load=skip_load,
                    strict=strict,
                )
                self.warnings.extend(source_result.warnings)
                if source_result.errors:
                    source_errors.extend(source_result.errors)
                total_loaded += source_result.loaded
                total_invalid += source_result.invalid
                total_skipped += source_result.skipped
                total_failed += source_result.failed
                if source_result.did_work:
                    sources_succeeded += 1

                if strict and source_errors:
                    break

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
                "_sources_succeeded": sources_succeeded,
                "_wrote_data": not skip_load and sources_succeeded > 0,
            }

            if source_errors:
                stage_result.success = False
                stage_result.error = "; ".join(source_errors)
            elif total_failed > 0:
                stage_result.success = False
                stage_result.error = f"{total_failed} source processing failure(s)"

        stage_result.completed_at = datetime.now()
        return stage_result

    _resolve_schema_name = staticmethod(resolve_schema_name)

    def _process_gis_source(
        self,
        source: DataSource,
        *,
        preview: bool = False,
    ) -> dict[str, Any]:
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
            from .gis_loader import load_gis_parcels, translate_gis_parcels

            if preview:
                translated = translate_gis_parcels(str(shapefile_path))
                self.logger.info(f"Preview translated {len(translated)} GIS parcel coordinates")
                return {
                    "loaded": len(translated),
                    "invalid": 0,
                    "skipped": 0,
                    "failed": 0,
                }

            count = load_gis_parcels(str(shapefile_path), refresh_readiness=False)
            self.logger.info(f"Updated {count} properties with GIS coordinates")
            return {"loaded": count, "invalid": 0, "skipped": 0, "failed": 0}

        except ImportError as e:
            self.logger.error(f"GIS processing requires geopandas: {e}")
            return {"loaded": 0, "invalid": 0, "skipped": 0, "failed": 1, "source_error": str(e)}
        except (DatabaseError, OSError, ValueError) as e:
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


def run_harris_import(
    request: HarrisImportRequest,
    *,
    reporter: HarrisImportReporter | None = None,
    reviewer=None,
) -> HarrisImportResult:
    """Run one Harris import through the catalog-owned, stateless boundary."""
    if not isinstance(request, HarrisImportRequest):
        raise InvalidHarrisImportRequest("request must be a HarrisImportRequest")
    data_year = _resolve_data_year(request)
    sources = request.plan.select_sources(DEFAULT_HCAD_SOURCE_CATALOG.required_sources())
    if not sources:
        raise InvalidHarrisImportRequest(
            f"No HCAD catalog sources match Harris import plan {request.plan.legacy_scope}"
        )
    operation = ImportOperation.objects.create(
        county="harris",
        intent="preview" if isinstance(request.load, HarrisPreview) else request.plan.legacy_scope,
        requested_year=data_year,
        actor=request.actor,
        origin=request.origin,
        evidence=requested_replay(request.replay),
    )
    try:
        with (
            import_warnings("etl_orchestrator", operation_id=str(operation.pk)) as warnings,
            county_writer(operation),
        ):
            if request.replay is not None:
                from .candidate import published_identity

                verify_replay(request.replay, operation, published_identity())
            if request.candidate_id is None:
                execution = _HarrisImportExecution(
                    request, sources, data_year, reporter=reporter, operation=operation
                )
            if request.candidate_id is not None:
                from .publication import publish_candidate

                operation.evidence["application_reason"] = request.application_reason

                candidate = ImportCandidate.objects.get(pk=request.candidate_id, county="harris")
                if {key: candidate.request[key] for key in ("plan", "data_year")} != {
                    "plan": request.plan.legacy_scope,
                    "data_year": data_year,
                }:
                    raise InvalidHarrisImportRequest(
                        "Candidate request identity differs from application"
                    )
                publish_candidate(candidate.pk, operation, user=reviewer)
                result = HarrisImportResult(
                    status=HarrisImportStatus.COMPLETED,
                    started_at=datetime.now(),
                    completed_at=datetime.now(),
                    wrote_data="already_applied" not in operation.evidence,
                    already_applied="already_applied" in operation.evidence,
                    candidate_id=candidate.pk,
                )
            elif isinstance(request.load, HarrisApply):
                from .candidate import prepare_candidate

                execution.request = replace(
                    request,
                    load=HarrisPrepare(
                        refresh_readiness=request.load.refresh_readiness,
                        validate_completeness=request.load.validate_completeness,
                        extracted_source_retention=ExtractedSourceRetention.RETAIN,
                    ),
                )
                result = prepare_candidate(execution, operation)
            else:
                result = execution.run()
    except Exception as exc:
        operation.refresh_from_db(fields=["status", "publication_before", "publication_after"])
        if operation.status == "published":
            operation.warnings.append(str(exc))
            operation.finished_at = timezone.now()
            operation.save()
            return HarrisImportResult(
                status=HarrisImportStatus.COMPLETED,
                started_at=datetime.now(),
                completed_at=datetime.now(),
                wrote_data=True,
                operation_id=operation.pk,
                candidate_id=request.candidate_id,
                warnings=(str(exc),),
            )
        if operation.status != "published":
            operation.status = "failed"
        operation.errors = [str(exc)]
        operation.warnings = list(dict.fromkeys([*operation.warnings, *warnings]))
        operation.finished_at = timezone.now()
        operation.save()
        raise
    result = replace(result, operation_id=operation.pk)
    operation.status = (
        "already_applied"
        if result.already_applied
        else "published" if result.wrote_data else result.status.value
    )
    operation.evidence = {
        **operation.evidence,
        "result": result.to_dict(),
        "wrote_data": result.wrote_data,
        "qualified_publication": operation.evidence.get(
            "qualified_publication", "Published data unchanged"
        ),
    }
    operation.warnings = list(dict.fromkeys([*operation.warnings, *warnings, *result.warnings]))
    operation.errors = list(result.errors)
    operation.finished_at = timezone.now()
    operation.save()
    if (
        isinstance(request.load, HarrisApply)
        and not isinstance(request.load, HarrisPrepare)
        and request.candidate_id is None
        and result.status is HarrisImportStatus.PREPARED
    ):
        from .publication import publish_candidate

        candidate = ImportCandidate.objects.get(pk=result.candidate_id, county="harris")
        operation.evidence["application_reason"] = request.application_reason
        operation.save()
        publish_candidate(candidate.pk, operation, user=reviewer)
        result = replace(
            result,
            status=HarrisImportStatus.COMPLETED,
            wrote_data="already_applied" not in operation.evidence,
            already_applied="already_applied" in operation.evidence,
            completed_at=datetime.now(),
        )
        operation.status = (
            "already_applied"
            if result.already_applied
            else "published" if result.wrote_data else result.status.value
        )
        operation.evidence = {
            **operation.evidence,
            "result": result.to_dict(),
            "wrote_data": result.wrote_data,
            "qualified_publication": operation.evidence.get(
                "qualified_publication", "Published data unchanged"
            ),
        }
        operation.finished_at = timezone.now()
        operation.save()
        return result
    return result


def _resolve_data_year(request: HarrisImportRequest) -> int:
    raw_year: int | str = request.data_year or os.getenv("ETL_DATA_YEAR") or datetime.now().year
    try:
        data_year = int(raw_year)
    except (TypeError, ValueError) as exc:
        raise InvalidHarrisImportRequest(f"Invalid Harris import data year: {raw_year}") from exc
    if data_year < 2000:
        raise InvalidHarrisImportRequest("Harris import data year must be 2000 or later")
    return data_year


__all__ = [
    "ExtractedSourceRetention",
    "HarrisAcquisitionMode",
    "HarrisApply",
    "HarrisPrepare",
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
