"""Guarded coordinate-only enrichment for an active Partial Brazos snapshot."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.db import transaction

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


class CoordinateEnrichmentError(Exception):
    """Base failure translated by the management-command adapter."""


class InvalidCoordinateEnrichmentRequest(CoordinateEnrichmentError, ValueError):
    """The request is invalid before source acquisition starts."""


class CoordinateEnrichmentSourceError(CoordinateEnrichmentError):
    """The selected GIS source could not be prepared or interpreted."""


@dataclass(frozen=True)
class CoordinateEnrichmentResult:
    """Immutable outcome, evidence, and audit identity for one invocation."""

    audit_id: int
    outcome: CoordinateEnrichmentOutcome
    report: CoordinateEnrichmentReport
    updated_count: int
    cleanup_state: CoordinateCleanupState
    reason: str = ""


@dataclass(frozen=True)
class _CoordinateAnalysis:
    """Prepared source details that never cross the external seam."""

    preparation: StagePreparation
    report: CoordinateEnrichmentReport
    candidates: dict[str, tuple[Decimal, Decimal]]


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
        return self._analyze(request).report

    def apply(
        self,
        request: CoordinateEnrichmentRequest,
        *,
        minimum_match_rate: float,
    ) -> CoordinateEnrichmentResult:
        """Apply one independently staged and measured coordinate update."""
        self._validate_request(request)
        self._validate_minimum_match_rate(minimum_match_rate)
        analysis = self._analyze(request)
        preparation = analysis.preparation
        report = analysis.report
        candidates = analysis.candidates
        snapshot = self._active_partial_snapshot(report.target_year)

        rejection = self._rejection_reason(snapshot, report, minimum_match_rate)
        if rejection:
            audit = self._record_audit(
                snapshot=snapshot,
                report=report,
                outcome=CoordinateEnrichmentOutcome.REJECTED,
                minimum_match_rate=minimum_match_rate,
                updated_count=0,
                reason=rejection,
            )
            return self._result(audit, report)

        audit, outcome = self._apply_with_audit(
            snapshot=snapshot,
            report=report,
            candidates=candidates,
            minimum_match_rate=minimum_match_rate,
        )
        if outcome in {CoordinateEnrichmentOutcome.APPLIED, CoordinateEnrichmentOutcome.NOOP}:
            self._finalize_cleanup(audit, preparation, request)
        audit.refresh_from_db()
        return self._result(audit, report)

    def _analyze(self, request: CoordinateEnrichmentRequest) -> _CoordinateAnalysis:
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
        accounts_by_prop_id = {
            account.prop_id: account
            for account in PropertyAccount.objects.filter(tax_year=preparation.target_year)
        }
        matched_ids = candidates.keys() & accounts_by_prop_id.keys()
        report = CoordinateEnrichmentReport(
            source_year=preparation.source_year,
            target_year=preparation.target_year,
            target_accounts=len(accounts_by_prop_id),
            matched_accounts=len(matched_ids),
            unmatched_target_accounts=len(accounts_by_prop_id) - len(matched_ids),
            unmatched_source_ids=len(candidates) - len(matched_ids),
            **source_metrics,
        )
        return _CoordinateAnalysis(
            preparation=preparation,
            report=report,
            candidates=candidates,
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
    def _active_partial_snapshot(target_year: int) -> BrazosPropertySnapshot | None:
        return BrazosPropertySnapshot.objects.filter(
            is_active=True,
            tax_year=target_year,
            outcome=SnapshotOutcome.PARTIAL,
        ).first()

    @staticmethod
    def _rejection_reason(
        snapshot: BrazosPropertySnapshot | None,
        report: CoordinateEnrichmentReport,
        minimum_match_rate: float | None,
    ) -> str:
        if snapshot is None:
            return "Target year is not the active Partial Brazos property snapshot."
        if not report.target_accounts:
            return "The target CAD year has no PropertyAccount rows."
        if not report.usable_coordinate_records:
            return "The GIS source has no usable coordinates."
        if report.duplicate_source_ids:
            return "Normalized source PROP_IDs are duplicated."
        if minimum_match_rate is not None and report.match_rate < minimum_match_rate:
            return "Measured match rate is below the required threshold."
        return ""

    def _apply_with_audit(
        self,
        *,
        snapshot: BrazosPropertySnapshot | None,
        report: CoordinateEnrichmentReport,
        candidates: dict[str, tuple[Decimal, Decimal]],
        minimum_match_rate: float | None,
    ) -> tuple[CoordinateEnrichmentAudit, CoordinateEnrichmentOutcome]:
        with transaction.atomic():
            locked_snapshot = (
                BrazosPropertySnapshot.objects.select_for_update()
                .filter(
                    pk=snapshot.pk if snapshot else None,
                    is_active=True,
                    tax_year=report.target_year,
                    outcome=SnapshotOutcome.PARTIAL,
                )
                .first()
            )
            if locked_snapshot is None:
                audit = self._record_audit(
                    snapshot=None,
                    report=report,
                    outcome=CoordinateEnrichmentOutcome.REJECTED,
                    minimum_match_rate=minimum_match_rate,
                    updated_count=0,
                    reason="Target snapshot changed before coordinate enrichment could apply.",
                )
                return audit, CoordinateEnrichmentOutcome.REJECTED

            updates: list[PropertyAccount] = []
            for account in PropertyAccount.objects.select_for_update().filter(
                tax_year=report.target_year
            ):
                coordinates = candidates.get(account.prop_id)
                if coordinates is None or not self._can_improve_source(account, report.source_year):
                    continue
                account.latitude, account.longitude = coordinates
                account.coordinate_source = COORDINATE_SOURCE
                account.coordinate_source_year = report.source_year
                updates.append(account)

            if updates:
                PropertyAccount.objects.bulk_update(
                    updates, COORDINATE_FIELDS_UPDATED, batch_size=200
                )
                outcome = CoordinateEnrichmentOutcome.APPLIED
            else:
                outcome = CoordinateEnrichmentOutcome.NOOP
            audit = self._record_audit(
                snapshot=locked_snapshot,
                report=report,
                outcome=outcome,
                minimum_match_rate=minimum_match_rate,
                updated_count=len(updates),
                reason="" if updates else "Existing coordinate provenance is equal or better.",
            )
            return audit, outcome

    @staticmethod
    def _can_improve_source(account: PropertyAccount, source_year: int) -> bool:
        if account.latitude is None or account.longitude is None:
            return True
        return (
            account.coordinate_source_year is not None
            and account.coordinate_source_year < source_year
        )

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
            self._source_stage.cleanup(preparation)
        except Exception:
            audit.cleanup_state = CoordinateCleanupState.FAILED
        else:
            audit.cleanup_state = CoordinateCleanupState.CLEANED
        audit.save(update_fields=["cleanup_state"])

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
