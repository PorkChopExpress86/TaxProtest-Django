"""County-owned publication of one Brazos detailed property snapshot.

The module owns source-year checks, CAD preflight, candidate staging,
coverage qualification, transactional publication, recovery staging,
and cleanup. CAD and GIS source stages remain their own adapters.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from django.conf import settings
from django.core.management.base import CommandError

from counties.brazos.models import (
    BrazosPropertySnapshot,
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyLand,
    SnapshotOutcome,
)
from counties.brazos.readiness import BrazosActiveSnapshotReadiness
from counties.common import candidate_ports
from counties.common.candidate_lifecycle import CandidateLoad, Loaded, prepare, publish
from counties.common.candidate_ports import CandidateTables
from counties.common.import_audit import OperationStatus, audited_operation
from counties.common.import_coverage import OutcomePopulation
from counties.common.import_recovery import (
    ReplayRejected,
    ReplayRequest,
    copy_exact_source,
    requested_replay,
    verify_replay,
)
from counties.common.import_writers import fenced_write, working_source_root
from counties.common.models import ImportCandidate, ImportOperation
from counties.common.tax_models import PropertyJurisdictionExemption


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

    def inspect(self, preparation: StagePreparation, operation: ImportOperation) -> StageResult: ...

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


PROPERTY_MODELS = (
    PropertyAccount,
    PropertyLand,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
)
MODELS = (*PROPERTY_MODELS, PropertyJurisdictionExemption, BrazosPropertySnapshot)


class BrazosCandidatePort:
    """What the shared Candidate lifecycle asks Brazos."""

    tables = CandidateTables(MODELS, county_scoped=frozenset({PropertyJurisdictionExemption}))

    def published(self) -> dict:
        snapshot = BrazosPropertySnapshot.objects.filter(is_active=True).first()
        return {
            "snapshot_id": snapshot.pk if snapshot else None,
            "tax_year": snapshot.tax_year if snapshot else None,
            "outcome": snapshot.outcome if snapshot else None,
        }

    def outcomes(self, candidate: ImportCandidate | None) -> dict[str, OutcomePopulation]:
        if candidate is None:
            return outcome_populations()
        partial = candidate.request["mode"] == PropertyImportMode.CAD_RECOVERY.value
        return outcome_populations(claimed_gis=not partial, deliberately_absent_gis=partial)


def dataset_identity(schema: str = "public") -> dict:
    return candidate_ports.dataset_identity("brazos", schema)


def published_identity() -> dict:
    return candidate_ports.published_identity("brazos")


def outcome_populations(
    *, claimed_gis=False, deliberately_absent_gis=False
) -> dict[str, OutcomePopulation]:
    reader = BrazosActiveSnapshotReadiness()
    snapshot = reader.active_snapshot()
    names = ("search", "comparable", "report", "tax")
    eligible = {name: set() for name in names}
    exclusions = {name: {} for name in names}
    accounts = list(PropertyAccount.objects.filter(tax_year=snapshot.tax_year)) if snapshot else []
    for account in accounts:
        projection = reader.project(account.prop_id)
        if projection is None:
            continue
        for name, ready in (
            ("search", projection.search_ready),
            ("comparable", projection.comparable_ready),
            ("report", projection.report_ready),
            ("tax", projection.tax_impact_ready),
        ):
            if ready and not (deliberately_absent_gis and name != "search"):
                eligible[name].add(account.prop_id)
            else:
                exclusions[name][account.prop_id] = [
                    projection.reason_for(name)
                    or "GIS deliberately absent from explicit Partial candidate"
                ]
    structural_inputs = any(account.living_area is not None for account in accounts) or bool(
        snapshot
        and PropertyLand.objects.filter(tax_year=snapshot.tax_year, acreage__isnull=False).exists()
    )
    has_gis = claimed_gis or any(
        account.coordinate_source and account.coordinate_source_year is not None
        for account in accounts
    )
    comparable_supported = has_gis and structural_inputs and not deliberately_absent_gis
    report_supported = (
        comparable_supported
        and len(accounts) >= 4
        and any(
            account.assessed_value is not None and account.living_area is not None
            for account in accounts
        )
    )
    # A tax gap is independently unavailable. The authoritative projection
    # requires complete matching-year rates and values before claiming it.
    tax_supported = bool(eligible["tax"]) and not deliberately_absent_gis
    supported = {
        "search": snapshot is not None,
        "comparable": comparable_supported,
        "report": report_supported,
        "tax": tax_supported,
    }
    reasons = {
        "search": "Active property snapshot unavailable",
        "comparable": "GIS or structural comparable prerequisites unavailable",
        "report": "Equity facts and at least three qualifying comparables are required",
        "tax": "Complete matching-year report, jurisdiction, values and rates are required",
    }
    return {
        name: OutcomePopulation(
            eligible[name],
            supported=supported[name],
            reason="" if supported[name] else reasons[name],
            exclusions=exclusions[name],
            deliberately_absent=deliberately_absent_gis and name != "search",
        )
        for name in names
    }


class _CandidateSource:
    """Keep actual county source inspection beside candidate-only persistence."""

    def __init__(self, stage: AnnualRefreshStage, operation: ImportOperation):
        self.stage, self.operation, self.name = stage, operation, stage.name

    def prepare(self, options):
        preparation = self.stage.prepare(options)
        measured = self.stage.inspect(preparation, self.operation)
        self.operation.evidence.setdefault("inspection", {})[self.name] = dict(measured.metrics)
        return preparation

    def persist(self, preparation):
        return self.stage.persist(preparation)

    def cleanup(self, preparation):
        return None


def brazos_candidate_load(
    cad: AnnualRefreshStage, gis: AnnualRefreshStage | None, request: PropertyImportRequest
) -> CandidateLoad:
    """Load the requested Brazos snapshot into a staged candidate schema."""

    def run(candidate: ImportCandidate, operation: ImportOperation) -> Loaded:
        operation.evidence["validation"] = {
            "valid": False,
            "database_publication": "Database publication untested",
        }
        importer = BrazosPropertyImport(
            _CandidateSource(cad, operation), _CandidateSource(gis, operation) if gis else None
        )
        result = importer._run_unstaged(
            replace(
                request,
                prepare_only=False,
                replay=None,
                options=replace(request.options, keep_extracted=True),
            ),
            operation,
        )
        operation.evidence["validation"]["valid"] = True
        operation.requested_year = result.tax_year
        return Loaded(
            complete=True,
            identity={"tax_year": result.tax_year},
            evidence={
                "population": {
                    model._meta.model_name: model.objects.filter(tax_year=result.tax_year).count()
                    for model in PROPERTY_MODELS
                },
                "coordinate_provenance": list(
                    PropertyAccount.objects.filter(tax_year=result.tax_year)
                    .exclude(coordinate_source="")
                    .values("prop_id", "coordinate_source", "coordinate_source_year")
                ),
                "outcome": result.outcome.value,
                "tax_year": result.tax_year,
                "cad": dict(result.cad.metrics) if result.cad else None,
                "gis": dict(result.gis.metrics) if result.gis else None,
                "inspection": operation.evidence.get("inspection", {}),
            },
            audit={
                "capabilities": (
                    "GIS capabilities unavailable"
                    if result.gis is None
                    else "Year-matched GIS inspected"
                )
            },
            result=result,
        )

    return CandidateLoad(
        identity={"mode": request.mode.value, "tax_year": request.options.tax_year},
        # GIS recovery keeps the published CAD rows, so inherits their sources.
        carries_published=request.mode is PropertyImportMode.GIS_RECOVERY,
        run=run,
    )


def _copy_archive(sources):
    item = next((item for item in sources if Path(item["path"]).suffix.lower() == ".zip"), None)
    if item is None:
        return None
    destination = (
        working_source_root(Path(settings.BCAD_DOWNLOAD_DIR), reuse=False) / Path(item["path"]).name
    )
    copy_exact_source(item, destination)
    return destination


def prepare_recovery(candidate, replay, *, actor):
    from counties.brazos.cad_refresh import ALL_FILENAMES, CadRefreshStage, _CadStagePayload
    from counties.brazos.gis_refresh import GisRefreshStage, GisSourcePayload

    class RetainedCadStage(CadRefreshStage):
        def __init__(self, sources):
            super().__init__()
            self.sources = [item for item in sources if item.get("stage") == "cad"]

        def prepare(self, options):
            if not self.sources:
                raise ReplayRejected("Retained CAD sources are unavailable")
            year = self.sources[0]["source_year"]
            root = working_source_root(Path(settings.BCAD_EXTRACT_DIR), reuse=False) / str(year)
            files = {}
            for filename in ALL_FILENAMES:
                item = next(
                    (
                        item
                        for item in self.sources
                        if Path(item["path"]).name.upper().endswith(filename)
                    ),
                    None,
                )
                if item is None:
                    raise ReplayRejected(f"Retained CAD input unavailable: {filename}")
                destination = root / Path(item["path"]).name
                copy_exact_source(item, destination)
                files[filename] = destination
            archive = _copy_archive(self.sources)
            return StagePreparation(
                "cad",
                year,
                options.tax_year or year,
                _CadStagePayload(files, root, archive, self.sources[0].get("source_url") or ""),
                (),
            )

    class RetainedGisStage(GisRefreshStage):
        def __init__(self, sources):
            super().__init__()
            self.sources = [item for item in sources if item.get("stage") == "gis"]

        def prepare(self, options):
            shape = next(
                (item for item in self.sources if Path(item["path"]).suffix.lower() == ".shp"), None
            )
            if shape is None:
                raise ReplayRejected("Retained GIS layer unavailable")
            original = Path(shape["path"])
            year = shape["source_year"]
            root = (
                working_source_root(Path(settings.BCAD_EXTRACT_DIR), reuse=False)
                / "gis"
                / str(year)
            )
            for item in self.sources:
                path = Path(item["path"])
                if path.parent == original.parent and path.stem == original.stem:
                    copy_exact_source(item, root / path.name)
            archive = _copy_archive(self.sources)
            return StagePreparation(
                "gis",
                year,
                options.tax_year or year,
                GisSourcePayload(
                    root / original.name, root, archive, shape.get("source_url") or ""
                ),
                (),
            )

    partial = candidate.evidence["outcome"] == "partial"
    return BrazosPropertyImport(
        RetainedCadStage(candidate.sources),
        None if partial else RetainedGisStage(candidate.sources),
    ).run(
        PropertyImportRequest(
            mode=PropertyImportMode.CAD_RECOVERY if partial else PropertyImportMode.ANNUAL,
            options=RefreshOptions(
                tax_year=candidate.request["tax_year"], skip_download=True, skip_extract=True
            ),
            prepare_only=True,
            actor=actor,
            origin="admin_recovery",
            replay=replay,
        )
    )


class BrazosPropertyImport:
    """Prepare and publish a completed or Partial Brazos property snapshot."""

    def __init__(self, cad: AnnualRefreshStage, gis: AnnualRefreshStage | None):
        self._cad = cad
        self._gis = gis

    def run(self, request: PropertyImportRequest, *, reviewer=None) -> PropertyImportResult:
        active = BrazosPropertySnapshot.objects.filter(is_active=True).first()
        result: PropertyImportResult | None = None
        with audited_operation(
            "brazos",
            "preview" if request.options.dry_run else request.mode.value,
            actor=request.actor,
            origin=request.origin,
            requested_year=request.options.tax_year,
            evidence=requested_replay(request.replay),
            publication_before=(
                {"snapshot_id": active.pk, "tax_year": active.tax_year} if active else None
            ),
        ) as operation:
            if request.replay is not None:
                verify_replay(request.replay, operation, published_identity())
            if request.candidate_id is not None:
                candidate = ImportCandidate.objects.get(pk=request.candidate_id, county="brazos")
                if candidate.request != {
                    "mode": request.mode.value,
                    "tax_year": request.options.tax_year or candidate.request["tax_year"],
                }:
                    raise ValueError("Candidate request identity differs from application")
                operation.requested_year = candidate.request["tax_year"]
                publish(operation, candidate.pk, user=reviewer, reason=request.application_reason)
                active = BrazosPropertySnapshot.objects.get(is_active=True)
                already = "already_applied" in operation.evidence
                completed = PropertyImportResult(
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
            elif request.options.dry_run:
                completed = self._preview(request, operation)
            else:
                # An automatic publication runs under the reservation that prepared it.
                prepared = prepare(
                    operation,
                    brazos_candidate_load(self._cad, self._gis, request),
                    automatic_publication=not request.prepare_only,
                    user=reviewer,
                    reason=request.application_reason,
                )
                completed = replace(
                    prepared.result,
                    snapshot_id=None,
                    prepared=True,
                    candidate_id=prepared.candidate.pk,
                    workflow_state=prepared.candidate.state,
                )
                if prepared.candidate.state == "published":
                    active = BrazosPropertySnapshot.objects.get(is_active=True)
                    completed = replace(
                        completed,
                        tax_year=active.tax_year,
                        outcome=PropertyImportOutcome(active.outcome),
                        snapshot_id=active.pk,
                        prepared=False,
                    )
            result = replace(completed, operation_id=operation.pk)
            operation.status = (
                OperationStatus.COMPLETED
                if result.dry_run and not result.prepared
                else OperationStatus(result.workflow_state)
            )
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
            operation.warnings.extend(result.cleanup_warnings)
        if result is None:
            # Publication committed; the operation recorded the later failure as a warning.
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
                cleanup_warnings=(operation.evidence["post_publication_failure"],),
            )
        return result

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
        operation.evidence["validation"] = {
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
        operation.evidence["validation"]["valid"] = True
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


__all__ = [
    "MODELS",
    "PROPERTY_MODELS",
    "AnnualRefreshResult",
    "AnnualRefreshStage",
    "BrazosAnnualRefresh",
    "BrazosPropertyImport",
    "PropertyImportMode",
    "PropertyImportOutcome",
    "PropertyImportRequest",
    "PropertyImportResult",
    "PropertyImportStage",
    "RefreshOptions",
    "StagePreparation",
    "StageResult",
    "build_default_property_import",
    "build_default_refresh",
    "dataset_identity",
    "outcome_populations",
    "brazos_candidate_load",
    "prepare_recovery",
    "published_identity",
]
