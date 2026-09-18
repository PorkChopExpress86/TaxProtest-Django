"""County-owned publication of one Brazos detailed property snapshot.

The module owns source-year checks, CAD preflight, transactional publication,
and cleanup.  CAD and GIS source stages remain their own adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from uuid import UUID

from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from counties.brazos.annual_refresh import (
    AnnualRefreshStage,
    RefreshOptions,
    StagePreparation,
    StageResult,
)
from counties.brazos.models import BrazosPropertySnapshot, SnapshotOutcome
from counties.common.import_logging import import_warnings
from counties.common.import_writers import county_writer
from counties.common.models import ImportOperation


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


class BrazosPropertyImport:
    """Prepare and publish a completed or Partial Brazos property snapshot."""

    def __init__(self, cad: AnnualRefreshStage, gis: AnnualRefreshStage | None):
        self._cad = cad
        self._gis = gis

    def run(self, request: PropertyImportRequest) -> PropertyImportResult:
        active = BrazosPropertySnapshot.objects.filter(is_active=True).first()
        operation = ImportOperation.objects.create(
            county="brazos",
            intent="preview" if request.options.dry_run else request.mode.value,
            requested_year=request.options.tax_year,
            actor=request.actor,
            origin=request.origin,
            publication_before=(
                {"snapshot_id": active.pk, "tax_year": active.tax_year} if active else None
            ),
        )
        try:
            with import_warnings("brazos_cad") as warnings, county_writer(operation):
                result = self._run(request)
        except Exception as exc:
            operation.status = "failed"
            operation.errors = [str(exc)]
            operation.warnings = warnings
            operation.finished_at = timezone.now()
            operation.save()
            raise
        result = replace(result, operation_id=operation.pk)
        operation.status = "completed"
        operation.publication_after = (
            {"snapshot_id": result.snapshot_id, "tax_year": result.tax_year}
            if result.snapshot_id
            else operation.publication_before
        )
        operation.evidence = {
            "outcome": result.outcome.value,
            "dry_run": result.dry_run,
            "cad": dict(result.cad.metrics) if result.cad else None,
            "gis": dict(result.gis.metrics) if result.gis else None,
            "qualified_publication": "Not yet verified",
        }
        operation.warnings = list(dict.fromkeys([*warnings, *result.cleanup_warnings]))
        operation.finished_at = timezone.now()
        operation.save()
        return result

    def _run(self, request: PropertyImportRequest) -> PropertyImportResult:
        if request.mode is PropertyImportMode.ANNUAL:
            return self._run_annual(request.options)
        if request.mode is PropertyImportMode.CAD_RECOVERY:
            return self._run_cad_recovery(request.options)
        if request.mode is PropertyImportMode.GIS_RECOVERY:
            return self._run_gis_recovery(request.options)
        raise CommandError(f"Unsupported Brazos property-import mode: {request.mode}.")

    def _run_annual(self, options: RefreshOptions) -> PropertyImportResult:
        gis = self._required_gis()
        cad_preparation = self._cad.prepare(options)
        target_year = options.tax_year or cad_preparation.target_year
        gis_preparation = gis.prepare(
            replace(options, tax_year=target_year, source_year=target_year)
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

        with transaction.atomic():
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
        preparation = self._cad.prepare(options)
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

        with transaction.atomic():
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
        preparation = gis.prepare(replace(options, tax_year=target_year, source_year=target_year))
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

        with transaction.atomic():
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
