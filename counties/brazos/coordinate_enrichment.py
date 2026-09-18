"""Guarded coordinate-only enrichment for an active Partial Brazos snapshot."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from counties.brazos.annual_refresh import RefreshOptions, StagePreparation
from counties.brazos.gis_coordinates import interpret_gis_coordinates
from counties.brazos.gis_refresh import GisRefreshStage, GisSourcePayload
from counties.brazos.models import (
    BrazosPropertySnapshot,
    CoordinateCleanupState,
    CoordinateEnrichmentAudit,
    PropertyAccount,
    SnapshotOutcome,
)
from counties.brazos.models import CoordinateEnrichmentOutcome as ModelCoordinateEnrichmentOutcome
from counties.brazos.stage_reporting import SilentStageReporter, StageReporter
from counties.common.import_audit import audited_operation, record_sources
from counties.common.import_writers import fenced_write

COORDINATE_SOURCE = "bcad-certified-gis"
COORDINATE_FIELDS_UPDATED = (
    "latitude",
    "longitude",
    "coordinate_source",
    "coordinate_source_year",
)

CoordinateEnrichmentOutcome = ModelCoordinateEnrichmentOutcome


@dataclass(frozen=True)
class CoordinateEnrichmentReport:
    """Measured evidence for a single GIS-to-CAD coordinate join."""

    source_year: int
    target_year: int
    source_records: int
    usable_coordinate_records: int
    distinct_source_ids: int
    duplicate_source_ids: int
    invalid_coordinate_records: int
    target_accounts: int
    matched_accounts: int
    unmatched_target_accounts: int
    unmatched_source_ids: int

    @property
    def match_rate(self) -> float:
        return self.matched_accounts / self.target_accounts if self.target_accounts else 0.0

    def evidence(self) -> dict[str, int | float]:
        """Safe aggregate evidence suitable for a durable audit record."""
        return {
            "source_records": self.source_records,
            "usable_coordinate_records": self.usable_coordinate_records,
            "distinct_source_ids": self.distinct_source_ids,
            "duplicate_source_ids": self.duplicate_source_ids,
            "invalid_coordinate_records": self.invalid_coordinate_records,
            "target_accounts": self.target_accounts,
            "matched_accounts": self.matched_accounts,
            "unmatched_target_accounts": self.unmatched_target_accounts,
            "unmatched_source_ids": self.unmatched_source_ids,
            "match_rate": self.match_rate,
        }


@dataclass(frozen=True)
class CoordinateEnrichmentRequest:
    """Immutable request crossing the coordinate-enrichment seam."""

    target_year: int
    expected_source_year: int | None = None
    force: bool = False
    skip_download: bool = False
    skip_extract: bool = False
    keep_extracted: bool = False
    actor: str = ""
    origin: str = "operator"


class CoordinateEnrichmentError(Exception):
    """Base failure translated by the management-command adapter."""


class InvalidCoordinateEnrichmentRequest(CoordinateEnrichmentError, ValueError):
    """The request is invalid before source acquisition starts."""


class CoordinateEnrichmentSourceError(CoordinateEnrichmentError):
    """The selected GIS source could not be prepared or interpreted."""


class CoordinateEnrichmentRejected(CoordinateEnrichmentError):
    """A measured target population failed one or more publication guards."""

    def __init__(
        self,
        report: CoordinateEnrichmentReport,
        violations: tuple[str, ...],
    ):
        self.report = report
        self.violations = violations
        super().__init__("; ".join(violations))


@dataclass(frozen=True)
class CoordinateEnrichmentResult:
    """Immutable outcome, evidence, and audit identity for one invocation."""

    audit_id: int
    outcome: CoordinateEnrichmentOutcome
    report: CoordinateEnrichmentReport
    updated_count: int
    cleanup_state: CoordinateCleanupState
    reason: str = ""


class CoordinateEnrichmentCleanupError(CoordinateEnrichmentError):
    """Cleanup failed after the coordinate application committed."""

    def __init__(
        self,
        outcome: CoordinateEnrichmentResult,
        retained_paths: tuple[Path, ...],
    ):
        self.outcome = outcome
        self.retained_paths = retained_paths
        super().__init__("Committed coordinate application; extracted source cleanup failed.")


@dataclass(frozen=True)
class _PreparedCoordinateSource:
    """Staged coordinate evidence that never crosses the external seam."""

    preparation: StagePreparation
    candidates: dict[str, tuple[Decimal, Decimal]]
    source_metrics: dict[str, int]


class BrazosCoordinateEnrichment:
    """Analyze and optionally apply a threshold-gated coordinate update."""

    def __init__(
        self,
        reporter: StageReporter | None = None,
    ):
        self._reporter = reporter or SilentStageReporter()
        self._source_stage = GisRefreshStage(self._reporter)

    @property
    def stdout(self):
        return self._reporter.stdout

    @staticmethod
    def _coordinate_candidates(
        shapefile_path: Path,
    ) -> tuple[dict[str, tuple[Decimal, Decimal]], dict[str, int]]:
        import geopandas as gpd

        gdf = gpd.read_file(shapefile_path)
        evidence = interpret_gis_coordinates(gdf)
        return dict(evidence.coordinates), {
            "source_records": evidence.source_records,
            "usable_coordinate_records": evidence.usable_coordinate_records,
            "distinct_source_ids": evidence.distinct_source_ids,
            "duplicate_source_ids": evidence.duplicate_source_ids,
            "invalid_coordinate_records": evidence.invalid_coordinate_records,
        }

    def analyze(self, request: CoordinateEnrichmentRequest) -> CoordinateEnrichmentReport:
        """Measure one source-to-target join without writing or cleaning anything."""
        self._validate_request(request)
        with audited_operation(
            "brazos",
            "coordinate_analysis",
            actor=request.actor,
            origin=request.origin,
            requested_year=request.target_year,
        ) as operation:
            with fenced_write():
                source = self._prepare_source(request)
            accounts = list(PropertyAccount.objects.filter(tax_year=request.target_year))
            report = self._report_for_accounts(source, accounts)
            self._record_operation_source(operation, source, report)
            return report

    def apply(
        self,
        request: CoordinateEnrichmentRequest,
        *,
        minimum_match_rate: float,
    ) -> CoordinateEnrichmentResult:
        """Apply one independently staged and measured coordinate update."""
        self._validate_request(request)
        self._validate_minimum_match_rate(minimum_match_rate)
        with audited_operation(
            "brazos",
            "coordinate_enrichment",
            actor=request.actor,
            origin=request.origin,
            requested_year=request.target_year,
        ) as operation:
            return self._apply(request, minimum_match_rate, operation)

    def _apply(self, request, minimum_match_rate, operation):
        with fenced_write():
            source = self._prepare_source(request)
        self._record_operation_source(operation, source)
        try:
            audit, report = self._apply_measured_population(
                source=source,
                minimum_match_rate=minimum_match_rate,
            )
        except CoordinateEnrichmentRejected as exc:
            operation.evidence["measured"] = exc.report.evidence()
            raise
        operation.evidence["measured"] = report.evidence()
        operation.publication_before = {
            "snapshot_id": audit.snapshot_id,
            "tax_year": report.target_year,
        }
        operation.publication_after = operation.publication_before
        operation.evidence.update(
            committed=True,
            coordinate_audit_id=audit.pk,
            updated_count=audit.updated_count,
            minimum_match_rate=minimum_match_rate,
        )
        try:
            self._finalize_cleanup(audit, source.preparation, request)
        except Exception as exc:
            audit.refresh_from_db()
            operation.status = "completed_with_warnings"
            raise CoordinateEnrichmentCleanupError(
                self._result(audit, report),
                self._retained_cleanup_paths(source.preparation),
            ) from exc
        audit.refresh_from_db()
        return self._result(audit, report)

    @staticmethod
    def _record_operation_source(operation, source, report=None):
        operation.evidence.update(
            source_year=source.preparation.source_year,
            target_year=source.preparation.target_year,
        )
        if report is not None:
            operation.evidence["measured"] = report.evidence()
        payload = source.preparation.payload
        record_sources(
            operation,
            list(payload.shapefile_path.parent.glob("*")),
            source_year=source.preparation.source_year,
            target_year=source.preparation.target_year,
        )

    def _prepare_source(self, request: CoordinateEnrichmentRequest) -> _PreparedCoordinateSource:
        options = RefreshOptions(
            tax_year=request.target_year,
            source_year=request.expected_source_year,
            force=request.force,
            skip_download=request.skip_download,
            skip_extract=request.skip_extract,
            keep_extracted=request.keep_extracted,
        )
        try:
            preparation = self._source_stage.prepare(options)
        except Exception as exc:
            raise CoordinateEnrichmentSourceError(str(exc)) from exc
        if preparation.source_year >= preparation.target_year:
            raise CoordinateEnrichmentSourceError(
                "Coordinate-only enrichment requires an earlier GIS source year than "
                f"the target CAD year; received GIS {preparation.source_year} and CAD "
                f"{preparation.target_year}. Use refresh_brazos_annual for a year-matched snapshot."
            )
        payload = preparation.payload
        if not isinstance(payload, GisSourcePayload) or payload.shapefile_path is None:
            raise CoordinateEnrichmentSourceError(
                "No BCAD GIS shapefile was prepared for coordinate analysis."
            )

        self.stdout.write(f"Reading {payload.shapefile_path} for coordinate coverage ...")
        try:
            candidates, source_metrics = self._coordinate_candidates(payload.shapefile_path)
        except Exception as exc:
            raise CoordinateEnrichmentSourceError(str(exc)) from exc
        return _PreparedCoordinateSource(
            preparation=preparation,
            candidates=candidates,
            source_metrics=source_metrics,
        )

    @staticmethod
    def _report_for_accounts(
        source: _PreparedCoordinateSource,
        accounts: list[PropertyAccount],
    ) -> CoordinateEnrichmentReport:
        accounts_by_prop_id = {account.prop_id: account for account in accounts}
        matched_ids = source.candidates.keys() & accounts_by_prop_id.keys()
        return CoordinateEnrichmentReport(
            source_year=source.preparation.source_year,
            target_year=source.preparation.target_year,
            target_accounts=len(accounts_by_prop_id),
            matched_accounts=len(matched_ids),
            unmatched_target_accounts=len(accounts_by_prop_id) - len(matched_ids),
            unmatched_source_ids=len(source.candidates) - len(matched_ids),
            **source.source_metrics,
        )

    @staticmethod
    def _validate_request(request: CoordinateEnrichmentRequest) -> None:
        if not isinstance(request, CoordinateEnrichmentRequest):
            raise InvalidCoordinateEnrichmentRequest(
                "request must be a CoordinateEnrichmentRequest"
            )
        if not isinstance(request.target_year, int) or isinstance(request.target_year, bool):
            raise InvalidCoordinateEnrichmentRequest("target_year must be an integer")
        if request.expected_source_year is not None and (
            not isinstance(request.expected_source_year, int)
            or isinstance(request.expected_source_year, bool)
        ):
            raise InvalidCoordinateEnrichmentRequest(
                "expected_source_year must be an integer when supplied"
            )
        if (
            request.expected_source_year is not None
            and request.expected_source_year >= request.target_year
        ):
            raise InvalidCoordinateEnrichmentRequest(
                "Coordinate enrichment requires an earlier GIS source year. "
                "Use refresh_brazos_annual for a year-matched snapshot."
            )
        for flag_name in ("force", "skip_download", "skip_extract", "keep_extracted"):
            if not isinstance(getattr(request, flag_name), bool):
                raise InvalidCoordinateEnrichmentRequest(f"{flag_name} must be a boolean")
        if request.force and request.skip_download:
            raise InvalidCoordinateEnrichmentRequest("force cannot be combined with skip_download")

    @staticmethod
    def _validate_minimum_match_rate(minimum_match_rate: float) -> None:
        if (
            not isinstance(minimum_match_rate, (int, float))
            or isinstance(minimum_match_rate, bool)
            or not math.isfinite(minimum_match_rate)
            or not 0 <= minimum_match_rate <= 1
        ):
            raise InvalidCoordinateEnrichmentRequest(
                "minimum_match_rate must be a finite number between 0 and 1"
            )

    @staticmethod
    def _rejection_violations(
        snapshot: BrazosPropertySnapshot | None,
        report: CoordinateEnrichmentReport,
        minimum_match_rate: float,
    ) -> tuple[str, ...]:
        violations: list[str] = []
        if snapshot is None:
            violations.append("Target year is not the active Partial Brazos property snapshot.")
        if not report.target_accounts:
            violations.append("The target CAD year has no PropertyAccount rows.")
        if not report.usable_coordinate_records:
            violations.append("The GIS source has no usable coordinates.")
        if report.duplicate_source_ids:
            violations.append("Normalized source PROP_IDs are duplicated.")
        if not report.matched_accounts:
            violations.append("Measured join has zero matched accounts.")
        if report.match_rate < minimum_match_rate:
            violations.append("Measured match rate is below the required threshold.")
        return tuple(violations)

    def _apply_measured_population(
        self,
        *,
        source: _PreparedCoordinateSource,
        minimum_match_rate: float,
    ) -> tuple[CoordinateEnrichmentAudit, CoordinateEnrichmentReport]:
        with fenced_write():
            locked_snapshot = (
                BrazosPropertySnapshot.objects.select_for_update()
                .filter(
                    is_active=True,
                    tax_year=source.preparation.target_year,
                    outcome=SnapshotOutcome.PARTIAL,
                )
                .first()
            )
            target_accounts = list(
                PropertyAccount.objects.select_for_update().filter(
                    tax_year=source.preparation.target_year
                )
            )
            report = self._report_for_accounts(source, target_accounts)
            violations = self._rejection_violations(
                locked_snapshot,
                report,
                minimum_match_rate,
            )
            if violations:
                raise CoordinateEnrichmentRejected(report, violations)

            matched_accounts: list[PropertyAccount] = []
            for account in target_accounts:
                coordinates = source.candidates.get(account.prop_id)
                if coordinates is None:
                    continue
                account.latitude, account.longitude = coordinates
                account.coordinate_source = COORDINATE_SOURCE
                account.coordinate_source_year = report.source_year
                matched_accounts.append(account)

            PropertyAccount.objects.bulk_update(
                matched_accounts,
                COORDINATE_FIELDS_UPDATED,
                batch_size=200,
            )
            audit = self._record_audit(
                snapshot=locked_snapshot,
                report=report,
                outcome=CoordinateEnrichmentOutcome.APPLIED,
                minimum_match_rate=minimum_match_rate,
                updated_count=len(matched_accounts),
                reason="",
            )
            return audit, report

    @staticmethod
    def _record_audit(
        *,
        snapshot: BrazosPropertySnapshot | None,
        report: CoordinateEnrichmentReport,
        outcome: CoordinateEnrichmentOutcome,
        minimum_match_rate: float | None,
        updated_count: int,
        reason: str,
    ) -> CoordinateEnrichmentAudit:
        return CoordinateEnrichmentAudit.objects.create(
            snapshot=snapshot,
            source_year=report.source_year,
            target_year=report.target_year,
            outcome=outcome,
            minimum_match_rate=(
                Decimal(str(minimum_match_rate)) if minimum_match_rate is not None else None
            ),
            evidence=report.evidence(),
            updated_count=updated_count,
            reason=reason,
        )

    def _finalize_cleanup(
        self,
        audit: CoordinateEnrichmentAudit,
        preparation: StagePreparation,
        request: CoordinateEnrichmentRequest,
    ) -> None:
        if request.keep_extracted or request.skip_extract:
            return
        try:
            with fenced_write():
                self._source_stage.cleanup(preparation)
            retained_paths = self._retained_cleanup_paths(preparation)
            if retained_paths:
                raise OSError(
                    "GIS cleanup returned without removing: "
                    + ", ".join(str(path) for path in retained_paths)
                )
        except Exception:
            audit.cleanup_state = CoordinateCleanupState.FAILED
            audit.save(update_fields=["cleanup_state"])
            raise
        else:
            audit.cleanup_state = CoordinateCleanupState.CLEANED
            audit.save(update_fields=["cleanup_state"])

    @staticmethod
    def _retained_cleanup_paths(preparation: StagePreparation) -> tuple[Path, ...]:
        return tuple(path for path in preparation.cleanup_paths if path.exists())

    @staticmethod
    def _result(
        audit: CoordinateEnrichmentAudit, report: CoordinateEnrichmentReport
    ) -> CoordinateEnrichmentResult:
        return CoordinateEnrichmentResult(
            audit_id=audit.pk,
            outcome=CoordinateEnrichmentOutcome(audit.outcome),
            report=report,
            updated_count=audit.updated_count,
            cleanup_state=CoordinateCleanupState(audit.cleanup_state),
            reason=audit.reason,
        )
