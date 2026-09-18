"""County-owned publication of one Brazos detailed property snapshot.

The module owns source-year checks, CAD preflight, transactional publication,
and cleanup.  CAD and GIS source stages remain their own adapters.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from django.core.management.base import CommandError
from django.utils import timezone

from counties.brazos.models import BrazosPropertySnapshot, SnapshotOutcome
from counties.common.import_logging import import_warnings
from counties.common.import_recovery import ReplayRequest, requested_replay, verify_replay
from counties.common.import_writers import county_writer, fenced_write
from counties.common.models import ImportCandidate, ImportOperation


@dataclass(frozen=True)
class RefreshOptions:
    """The narrow operator interface for a complete annual refresh or property import."""

    tax_year: int | None = None
    source_year: int | None = None
    force: bool = False
    skip_download: bool = False
    skip_extract: bool = False
    dry_run: bool = False
    keep_extracted: bool = False


@dataclass(frozen=True)
class StagePreparation:
    """A source stage prepared for persistence but not yet published."""

    name: str
    source_year: int
    target_year: int
    payload: object
    cleanup_paths: tuple[Path, ...]


@dataclass(frozen=True)
class StageResult:
    """The observable outcome of one persisted refresh stage."""

    name: str
    metrics: Mapping[str, int]


@dataclass(frozen=True)
class AnnualRefreshResult:
    """The stage-aware outcome of a completed or dry-run annual refresh."""

    tax_year: int
    cad: StageResult
    gis: StageResult
    dry_run: bool
    workflow_state: str = "published"
    candidate_id: str | None = None
    operation_id: str | None = None


class PropertyImportStage(Protocol):
    """A source-specific adapter used by the Brazos property import."""

    name: str

    def prepare(self, options: RefreshOptions) -> StagePreparation: ...

    def persist(self, preparation: StagePreparation) -> StageResult: ...

    def cleanup(self, preparation: StagePreparation) -> None: ...


AnnualRefreshStage = PropertyImportStage


class PropertyImportMode(StrEnum):
    """The command-adapter request kind accepted by the property import."""

    ANNUAL = "annual"
    CAD_RECOVERY = "cad_recovery"
    GIS_RECOVERY = "gis_recovery"


PropertyImportOutcome = SnapshotOutcome


@dataclass(frozen=True)
class PropertyImportRequest:
    """Immutable operator request crossing the property-import seam."""

    mode: PropertyImportMode
    options: RefreshOptions
    actor: str = ""
    origin: str = "operator"
    prepare_only: bool = False
    candidate_id: UUID | None = None
    application_reason: str = ""
    replay: ReplayRequest | None = None

    def __post_init__(self):
        if self.replay is not None and (
            not self.prepare_only or self.candidate_id is not None or self.options.dry_run
        ):
            raise ValueError("Dataset recovery requires fresh candidate preparation")
        if self.candidate_id is not None and (self.options.dry_run or self.prepare_only):
            raise ValueError("Candidate application requires explicit publication intent")


@dataclass(frozen=True)
class PropertyImportResult:
    """Immutable result of a successfully staged property import."""

    tax_year: int
    outcome: PropertyImportOutcome
    cad: StageResult | None
    gis: StageResult | None
    snapshot_id: int | None
    dry_run: bool
    cleanup_warnings: tuple[str, ...] = ()
    operation_id: UUID | None = None
    candidate_id: UUID | None = None
    prepared: bool = False
    workflow_state: str = "published"
    already_applied: bool = False


class BrazosPropertyImport:
    """Prepare and publish a completed or Partial Brazos property snapshot."""

    def __init__(self, cad: AnnualRefreshStage, gis: AnnualRefreshStage | None):
        self._cad = cad
        self._gis = gis

    def run(self, request: PropertyImportRequest, *, reviewer=None) -> PropertyImportResult:
        active = BrazosPropertySnapshot.objects.filter(is_active=True).first()
        operation = ImportOperation.objects.create(
            county="brazos",
            intent="preview" if request.options.dry_run else request.mode.value,
            requested_year=request.options.tax_year,
            actor=request.actor,
            origin=request.origin,
            evidence=requested_replay(request.replay),
            publication_before=(
                {"snapshot_id": active.pk, "tax_year": active.tax_year} if active else None
            ),
        )
        try:
            with import_warnings("brazos_cad") as warnings, county_writer(operation):
                if request.replay is not None:
                    from counties.brazos.property_candidate import published_identity

                    verify_replay(request.replay, operation, published_identity())
                if request.candidate_id is not None:
                    from counties.brazos.property_publication import publish_candidate
                    from counties.common.models import ImportCandidate

                    candidate = ImportCandidate.objects.get(
                        pk=request.candidate_id, county="brazos"
                    )
                    if candidate.request != {
                        "mode": request.mode.value,
                        "tax_year": request.options.tax_year or candidate.request["tax_year"],
                    }:
                        raise ValueError("Candidate request identity differs from application")
                    operation.requested_year = candidate.request["tax_year"]
                    operation.evidence["application_reason"] = request.application_reason
                    publish_candidate(candidate.pk, operation, user=reviewer)
                    active = BrazosPropertySnapshot.objects.get(is_active=True)
                    already = "already_applied" in operation.evidence
                    result = PropertyImportResult(
                        tax_year=active.tax_year,
                        outcome=PropertyImportOutcome(active.outcome),
                        cad=(
                            StageResult("cad", candidate.evidence["cad"])
                            if candidate.evidence["cad"]
                            else None
                        ),
                        gis=(
                            StageResult("gis", candidate.evidence["gis"])
                            if candidate.evidence["gis"]
                            else None
                        ),
                        snapshot_id=active.pk,
                        dry_run=False,
                        candidate_id=candidate.pk,
                        workflow_state="already_applied" if already else "published",
                        already_applied=already,
                    )
                else:
                    result = self._run(request, operation)
                    if (
                        not request.prepare_only
                        and not request.options.dry_run
                        and result.workflow_state == "prepared"
                    ):
                        from counties.brazos.property_publication import publish_candidate

                        candidate = ImportCandidate.objects.get(
                            pk=result.candidate_id, county="brazos"
                        )
                        operation.requested_year = candidate.request["tax_year"]
                        operation.evidence["application_reason"] = request.application_reason
                        publish_candidate(candidate.pk, operation, user=reviewer)
                        active = BrazosPropertySnapshot.objects.get(is_active=True)
                        result = PropertyImportResult(
                            tax_year=active.tax_year,
                            outcome=PropertyImportOutcome(active.outcome),
                            cad=result.cad,
                            gis=result.gis,
                            snapshot_id=active.pk,
                            dry_run=False,
                            candidate_id=candidate.pk,
                            workflow_state="published",
                            already_applied=False,
                            cleanup_warnings=result.cleanup_warnings,
                        )
        except Exception as exc:
            operation.refresh_from_db(fields=["status", "publication_before", "publication_after"])
            if operation.status == "published":
                operation.warnings.append(str(exc))
                operation.save()
                observed = operation.publication_after
                return PropertyImportResult(
                    tax_year=observed["tax_year"],
                    outcome=PropertyImportOutcome(observed["outcome"]),
                    cad=None,
                    gis=None,
                    snapshot_id=observed["snapshot_id"],
                    dry_run=False,
                    operation_id=operation.pk,
                    candidate_id=request.candidate_id,
                    cleanup_warnings=(str(exc),),
                )
            operation.status = "failed"
            operation.errors = [str(exc)]
            operation.warnings = list(dict.fromkeys([*operation.warnings, *warnings]))
            operation.finished_at = timezone.now()
            operation.save()
            raise
        result = replace(result, operation_id=operation.pk)
        if result.prepared:
            operation.status = result.workflow_state
        else:
            operation.status = "completed" if result.dry_run else result.workflow_state
        operation.publication_after = (
            {"snapshot_id": result.snapshot_id, "tax_year": result.tax_year}
            if result.snapshot_id
            else operation.publication_before
        )
        operation.evidence = {
            **operation.evidence,
            "outcome": result.outcome.value,
            "dry_run": result.dry_run,
            "cad": dict(result.cad.metrics) if result.cad else None,
            "gis": dict(result.gis.metrics) if result.gis else None,
            "qualified_publication": operation.evidence.get(
                "qualified_publication", "Published data unchanged"
            ),
        }
        operation.warnings = list(
            dict.fromkeys([*operation.warnings, *warnings, *result.cleanup_warnings])
        )
        operation.finished_at = timezone.now()
        operation.save()
        return result

    def _run(
        self, request: PropertyImportRequest, operation: ImportOperation
    ) -> PropertyImportResult:
        if request.options.dry_run:
            return self._preview(request, operation)
        from counties.brazos.property_candidate import prepare_candidate

        return prepare_candidate(self._cad, self._gis, request, operation)

    def _run_unstaged(
        self, request: PropertyImportRequest, operation: ImportOperation
    ) -> PropertyImportResult:
        if request.mode is PropertyImportMode.ANNUAL:
            return self._run_annual(request.options)
        if request.mode is PropertyImportMode.CAD_RECOVERY:
            return self._run_cad_recovery(request.options)
        if request.mode is PropertyImportMode.GIS_RECOVERY:
            return self._run_gis_recovery(request.options)
        raise CommandError(f"Unsupported Brazos property-import mode: {request.mode}.")

    def _preview(
        self, request: PropertyImportRequest, operation: ImportOperation
    ) -> PropertyImportResult:
        from counties.brazos.source_validation import inspect_cad, inspect_gis

        # Source preparation actually acquires/extracts the selected bytes. Only
        # persistence and cleanup are suppressed by a property preview.
        options = replace(request.options, dry_run=False)
        operation.evidence["source_validation"] = {
            "valid": False,
            "database_publication": "Database publication untested",
        }
        cad = gis = None
        if request.mode in (PropertyImportMode.ANNUAL, PropertyImportMode.CAD_RECOVERY):
            preparation = self._prepare(self._cad, options)
            target_year = options.tax_year or preparation.target_year
            cad = inspect_cad(operation, preparation)
            self._validate_source_year(target_year, preparation, "CAD")
        else:
            active = BrazosPropertySnapshot.objects.filter(is_active=True).first()
            target_year = options.tax_year or (active.tax_year if active else None)
            if (
                active is None
                or active.tax_year != target_year
                or active.outcome != PropertyImportOutcome.PARTIAL
            ):
                raise CommandError(
                    "GIS recovery requires the active Partial Brazos property snapshot."
                )
        if request.mode in (PropertyImportMode.ANNUAL, PropertyImportMode.GIS_RECOVERY):
            preparation = self._prepare(
                self._required_gis(),
                replace(options, tax_year=target_year, source_year=target_year),
            )
            gis = inspect_gis(operation, preparation)
            self._validate_source_year(target_year, preparation, "GIS")
        operation.evidence["source_validation"]["valid"] = True
        operation.evidence["capabilities"] = (
            "GIS capabilities unavailable" if gis is None else "Year-matched GIS inspected"
        )
        return PropertyImportResult(
            tax_year=target_year,
            outcome=(
                PropertyImportOutcome.PARTIAL if gis is None else PropertyImportOutcome.COMPLETED
            ),
            cad=cad,
            gis=gis,
            snapshot_id=None,
            dry_run=True,
            workflow_state="validated",
        )

    def _run_annual(self, options: RefreshOptions) -> PropertyImportResult:
        gis = self._required_gis()
        cad_preparation = self._prepare(self._cad, options)
        target_year = options.tax_year or cad_preparation.target_year
        gis_preparation = self._prepare(
            gis, replace(options, tax_year=target_year, source_year=target_year)
        )
        self._validate_cad(target_year, cad_preparation, inspect_source=not options.dry_run)
        self._validate_gis(target_year, gis_preparation)
        if options.dry_run:
            return PropertyImportResult(
                tax_year=target_year,
                outcome=PropertyImportOutcome.COMPLETED,
                cad=None,
                gis=None,
                snapshot_id=None,
                dry_run=True,
            )

        with fenced_write():
            cad_result = self._cad.persist(cad_preparation)
            gis_result = gis.persist(gis_preparation)
            snapshot = self._publish_snapshot(
                tax_year=target_year,
                outcome=PropertyImportOutcome.COMPLETED,
                cad_source_year=cad_preparation.source_year,
                gis_source_year=gis_preparation.source_year,
            )
        return PropertyImportResult(
            tax_year=target_year,
            outcome=PropertyImportOutcome.COMPLETED,
            cad=cad_result,
            gis=gis_result,
            snapshot_id=snapshot.id,
            dry_run=False,
            cleanup_warnings=self._cleanup(options, cad_preparation, gis_preparation),
        )

    def _run_cad_recovery(self, options: RefreshOptions) -> PropertyImportResult:
        preparation = self._prepare(self._cad, options)
        target_year = options.tax_year or preparation.target_year
        self._validate_cad(target_year, preparation, inspect_source=not options.dry_run)
        if options.dry_run:
            return PropertyImportResult(
                tax_year=target_year,
                outcome=PropertyImportOutcome.PARTIAL,
                cad=None,
                gis=None,
                snapshot_id=None,
                dry_run=True,
            )

        with fenced_write():
            cad_result = self._cad.persist(preparation)
            snapshot = self._publish_snapshot(
                tax_year=target_year,
                outcome=PropertyImportOutcome.PARTIAL,
                cad_source_year=preparation.source_year,
                gis_source_year=None,
            )
        return PropertyImportResult(
            tax_year=target_year,
            outcome=PropertyImportOutcome.PARTIAL,
            cad=cad_result,
            gis=None,
            snapshot_id=snapshot.id,
            dry_run=False,
            cleanup_warnings=self._cleanup(options, preparation),
        )

    def _run_gis_recovery(self, options: RefreshOptions) -> PropertyImportResult:
        gis = self._required_gis()
        active = BrazosPropertySnapshot.objects.filter(is_active=True).first()
        target_year = options.tax_year or (active.tax_year if active else None)
        if active is None or target_year is None:
            raise CommandError(
                "No active Partial Brazos property snapshot is available for GIS recovery."
            )
        if active.tax_year != target_year or active.outcome != PropertyImportOutcome.PARTIAL:
            raise CommandError(
                "GIS recovery requires the requested year to be the active Partial Brazos property snapshot."
            )
        preparation = self._prepare(
            gis, replace(options, tax_year=target_year, source_year=target_year)
        )
        self._validate_gis(target_year, preparation)
        if options.dry_run:
            return PropertyImportResult(
                tax_year=target_year,
                outcome=PropertyImportOutcome.COMPLETED,
                cad=None,
                gis=None,
                snapshot_id=None,
                dry_run=True,
            )

        with fenced_write():
            gis_result = gis.persist(preparation)
            snapshot = self._publish_snapshot(
                tax_year=target_year,
                outcome=PropertyImportOutcome.COMPLETED,
                cad_source_year=active.cad_source_year,
                gis_source_year=preparation.source_year,
            )
        return PropertyImportResult(
            tax_year=target_year,
            outcome=PropertyImportOutcome.COMPLETED,
            cad=None,
            gis=gis_result,
            snapshot_id=snapshot.id,
            dry_run=False,
            cleanup_warnings=self._cleanup(options, preparation),
        )

    @staticmethod
    def _prepare(stage: AnnualRefreshStage, options: RefreshOptions) -> StagePreparation:
        with fenced_write():
            return stage.prepare(options)

    @staticmethod
    def _validate_source_year(target_year: int, preparation: StagePreparation, label: str) -> None:
        if preparation.target_year != target_year or preparation.source_year != target_year:
            raise CommandError(
                f"{label} source year {preparation.source_year} does not match requested year {target_year}."
            )

    def _validate_cad(
        self, target_year: int, preparation: StagePreparation, *, inspect_source: bool
    ) -> None:
        self._validate_source_year(target_year, preparation, "CAD")
        validate_preflight = getattr(self._cad, "validate_preflight", None)
        if inspect_source and validate_preflight is not None:
            validate_preflight(preparation)

    def _validate_gis(self, target_year: int, preparation: StagePreparation) -> None:
        self._validate_source_year(target_year, preparation, "GIS")

    def _publish_snapshot(
        self,
        *,
        tax_year: int,
        outcome: PropertyImportOutcome,
        cad_source_year: int,
        gis_source_year: int | None,
    ) -> BrazosPropertySnapshot:
        active_snapshots = list(
            BrazosPropertySnapshot.objects.select_for_update().filter(is_active=True)
        )
        if active_snapshots:
            BrazosPropertySnapshot.objects.filter(
                pk__in=[row.pk for row in active_snapshots]
            ).update(is_active=False)
        return BrazosPropertySnapshot.objects.create(
            tax_year=tax_year,
            outcome=outcome,
            cad_source_year=cad_source_year,
            gis_source_year=gis_source_year,
            is_active=True,
        )

    def _cleanup(self, options: RefreshOptions, *preparations: StagePreparation) -> tuple[str, ...]:
        if options.keep_extracted:
            return ()
        warnings: list[str] = []
        stages = {self._cad.name: self._cad}
        if self._gis is not None:
            stages[self._gis.name] = self._gis
        for preparation in preparations:
            try:
                with fenced_write():
                    stages[preparation.name].cleanup(preparation)
            except Exception as exc:  # cleanup is post-commit and cannot revoke publication
                warnings.append(f"{preparation.name} cleanup failed: {exc}")
        return tuple(warnings)

    def _required_gis(self) -> AnnualRefreshStage:
        if self._gis is None:
            raise CommandError("This Brazos property import requires a GIS source stage.")
        return self._gis


def build_default_property_import(reporter: object) -> BrazosPropertyImport:
    """Build the county-owned property import from its two source adapters."""

    from counties.brazos.cad_refresh import CadRefreshStage
    from counties.brazos.gis_refresh import GisRefreshStage

    return BrazosPropertyImport(CadRefreshStage(reporter), GisRefreshStage(reporter))


class BrazosAnnualRefresh:
    """Strict command adapter for a completed property-import publication."""

    def __init__(self, cad: PropertyImportStage, gis: PropertyImportStage):
        self._cad = cad
        self._gis = gis

    def run(self, options: RefreshOptions) -> AnnualRefreshResult:
        result = BrazosPropertyImport(self._cad, self._gis).run(
            PropertyImportRequest(mode=PropertyImportMode.ANNUAL, options=options)
        )
        return AnnualRefreshResult(
            tax_year=result.tax_year,
            cad=result.cad or StageResult(name=self._cad.name, metrics={}),
            gis=result.gis or StageResult(name=self._gis.name, metrics={}),
            dry_run=result.dry_run,
            workflow_state=result.workflow_state,
            candidate_id=str(result.candidate_id) if result.candidate_id else None,
            operation_id=str(result.operation_id) if result.operation_id else None,
        )


def build_default_refresh(reporter: object) -> BrazosAnnualRefresh:
    from counties.brazos.cad_refresh import CadRefreshStage
    from counties.brazos.gis_refresh import GisRefreshStage

    return BrazosAnnualRefresh(CadRefreshStage(reporter), GisRefreshStage(reporter))

