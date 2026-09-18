"""The county-owned active-snapshot and readiness read module for Brazos."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from django.db.models import Q, Sum
from django.db.models.functions import Trim

from counties.brazos.models import (
    BrazosHistoricalCoverage,
    BrazosPropertySnapshot,
    HistoricalCoverageStatus,
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
    PropertyImprovement,
    PropertyLand,
)
from counties.brazos.similarity import find_similar_properties
from counties.common.analysis import assessment_history_rows
from counties.common.tax_models import AssessmentHistory, PropertyJurisdictionExemption, TaxUnitRate


@dataclass(frozen=True)
class BrazosReadinessProjection:
    """Immutable readiness facts for one property in the active snapshot."""

    snapshot_id: int
    tax_year: int
    prop_id: str
    owner_name: str
    search_ready: bool
    comparable_ready: bool
    report_ready: bool
    tax_impact_ready: bool
    comparison_mode: str | None
    coordinate_source: str
    coordinate_source_year: int | None
    reasons: tuple[tuple[str, str], ...]

    def reason_for(self, capability: str) -> str | None:
        return dict(self.reasons).get(capability)


class BrazosActiveSnapshotReadiness:
    """Resolve active Brazos records and their source-backed capabilities."""

    def active_snapshot(self) -> BrazosPropertySnapshot | None:
        return BrazosPropertySnapshot.objects.filter(is_active=True).first()

    def project(self, prop_id: str) -> BrazosReadinessProjection | None:
        snapshot = self.active_snapshot()
        if snapshot is None:
            return None
        projection = self.project_static(prop_id, snapshot=snapshot)
        account = self.account(prop_id, snapshot=snapshot)
        if projection is None or account is None:
            return None
        report_reason = self._report_reason(snapshot, account, projection)
        projection = self._with_capability(
            projection, "report", ready=report_reason is None, reason=report_reason
        )
        tax_reason = self._tax_reason(account, projection)
        return self._with_capability(projection, "tax", ready=tax_reason is None, reason=tax_reason)

    def project_static(
        self, prop_id: str, *, snapshot: BrazosPropertySnapshot | None = None
    ) -> BrazosReadinessProjection | None:
        """Resolve only per-record facts without calculating comparables or tax inputs."""
        snapshot = snapshot or self.active_snapshot()
        if snapshot is None:
            return None
        account = self.account(prop_id, snapshot=snapshot)
        if account is None:
            return None
        return self._project_account(snapshot, account)

    def account(
        self, prop_id: str, *, snapshot: BrazosPropertySnapshot | None = None
    ) -> PropertyAccount | None:
        snapshot = snapshot or self.active_snapshot()
        if snapshot is None:
            return None
        return PropertyAccount.objects.filter(prop_id=prop_id, tax_year=snapshot.tax_year).first()

    def search_queryset(self, *, snapshot: BrazosPropertySnapshot | None = None):
        snapshot = snapshot or self.active_snapshot()
        if snapshot is None:
            return PropertyAccount.objects.none()
        return (
            PropertyAccount.objects.filter(tax_year=snapshot.tax_year)
            .exclude(prop_id="")
            .annotate(
                _ready_owner_name=Trim("owner_name"),
                _ready_situs_address=Trim("situs_address"),
                _ready_mailing_address=Trim("mailing_address"),
            )
            .filter(
                Q(_ready_owner_name__gt="")
                | Q(_ready_situs_address__gt="")
                | Q(_ready_mailing_address__gt="")
            )
        )

    def comparable_results(
        self,
        prop_id: str,
        *,
        max_distance_miles: float,
        max_results: int,
        min_score: float,
        snapshot: BrazosPropertySnapshot | None = None,
    ) -> list[dict]:
        snapshot = snapshot or self.active_snapshot()
        if snapshot is None:
            return []
        account = self.account(prop_id, snapshot=snapshot)
        if account is None:
            return []
        subject = self._project_account(snapshot, account)
        if not subject.comparable_ready:
            return []
        results = find_similar_properties(
            prop_id,
            tax_year=snapshot.tax_year,
            max_distance_miles=max_distance_miles,
            max_results=max_results,
            min_score=min_score,
        )
        return [
            result
            for result in results
            if (
                (candidate := self._project_account(snapshot, result["property"])).comparable_ready
                and candidate.comparison_mode == subject.comparison_mode
            )
        ]

    def history_view(self, prop_id: str) -> list[dict]:
        """Return the active-year five-year window without collapsing gaps."""
        snapshot = self.active_snapshot()
        if snapshot is None:
            return []
        history_by_year = {
            row["tax_year"]: row
            for row in assessment_history_rows(prop_id, county="brazos", limit=50)
        }
        coverage_by_year = {
            row.tax_year: row
            for row in BrazosHistoricalCoverage.objects.filter(
                tax_year__gte=snapshot.tax_year - 4, tax_year__lte=snapshot.tax_year
            )
        }
        rows = []
        for year in range(snapshot.tax_year, snapshot.tax_year - 5, -1):
            entry = history_by_year.get(year)
            coverage = coverage_by_year.get(year)
            if entry is not None:
                rows.append({**entry, "coverage_status": "available", "coverage_reason": ""})
            elif coverage and coverage.status == HistoricalCoverageStatus.UNAVAILABLE:
                rows.append(
                    {
                        "tax_year": year,
                        "assessed_value": None,
                        "coverage_status": "unavailable",
                        "coverage_reason": coverage.failure_category,
                    }
                )
            else:
                rows.append(
                    {
                        "tax_year": year,
                        "assessed_value": None,
                        "coverage_status": "not_yet_assessed",
                        "coverage_reason": "",
                    }
                )
        return rows

    def _project_account(
        self, snapshot: BrazosPropertySnapshot, account: PropertyAccount
    ) -> BrazosReadinessProjection:
        reasons: dict[str, str] = {}
        # Keep the immutable projection and the queryset on the same trimmed
        # predicate. This avoids returning whitespace-only names or addresses
        # from search while marking them unavailable to the subject surface.
        search_ready = self.search_queryset(snapshot=snapshot).filter(pk=account.pk).exists()
        if not search_ready:
            reasons["search"] = "The active property record has no owner or address."

        has_coordinates = bool(
            account.latitude is not None
            and account.longitude is not None
            and account.coordinate_source
            and account.coordinate_source_year is not None
        )
        land_area = self._land_area(account)
        facts = self._comparison_facts(account)
        if not has_coordinates:
            mode = None
            reasons["comparable"] = "Coordinates with source provenance are unavailable."
        elif self._positive(account.living_area) and len(facts) >= 2:
            mode = "residential"
        elif self._positive(land_area):
            mode = "land"
        else:
            mode = None
            reasons["comparable"] = "The active property lacks enough comparable facts."

        comparable_ready = mode is not None
        reasons["report"] = "At least three same-mode comparable-ready properties are required."
        reasons["tax"] = "Report-ready evidence and matching-year tax inputs are required."
        return BrazosReadinessProjection(
            snapshot_id=snapshot.pk,
            tax_year=snapshot.tax_year,
            prop_id=account.prop_id,
            owner_name=account.owner_name,
            search_ready=search_ready,
            comparable_ready=comparable_ready,
            report_ready=False,
            tax_impact_ready=False,
            comparison_mode=mode,
            coordinate_source=account.coordinate_source,
            coordinate_source_year=account.coordinate_source_year,
            reasons=tuple(reasons.items()),
        )

    def _report_reason(
        self,
        snapshot: BrazosPropertySnapshot,
        account: PropertyAccount,
        projection: BrazosReadinessProjection,
    ) -> str | None:
        if not projection.comparable_ready:
            return projection.reason_for("comparable")
        if not self._positive(account.assessed_value) or not self._positive(account.living_area):
            return "Positive assessed value and living area are required for a report."
        comps = self.comparable_results(
            account.prop_id,
            max_distance_miles=10.0,
            max_results=50,
            min_score=30.0,
            snapshot=snapshot,
        )
        qualifying = [
            result
            for result in comps
            if self._positive(result["property"].assessed_value)
            and self._positive(result["property"].living_area)
        ]
        if len(qualifying) < 3:
            return "At least three same-mode comparable-ready properties with equity facts are required."
        return None

    @staticmethod
    def _with_capability(
        projection: BrazosReadinessProjection,
        capability: str,
        *,
        ready: bool,
        reason: str | None,
    ) -> BrazosReadinessProjection:
        reasons = dict(projection.reasons)
        if reason:
            reasons[capability] = reason
        else:
            reasons.pop(capability, None)
        if capability == "report":
            return replace(projection, report_ready=ready, reasons=tuple(reasons.items()))
        return replace(projection, tax_impact_ready=ready, reasons=tuple(reasons.items()))

    @classmethod
    def _tax_reason(
        cls, account: PropertyAccount, projection: BrazosReadinessProjection
    ) -> str | None:
        if not projection.report_ready:
            return projection.reason_for("report")
        rows = list(
            PropertyJurisdictionExemption.objects.filter(
                account_number=account.prop_id,
                tax_year=projection.tax_year,
                county="brazos",
            )
        )
        unit_codes = {row.tax_unit_code for row in rows if row.tax_unit_code}
        if not unit_codes:
            return "Matching-year jurisdiction and exemption rows are unavailable."
        bases = [row for row in rows if not row.exemption_code]
        if any(
            row.exemption_code and row.exemption_amount is None and row.exemption_percent is None
            for row in rows
        ):
            return "One or more matching-year exemption inputs are unverified."
        if {row.tax_unit_code for row in bases} != unit_codes:
            return "One or more matching-year gross jurisdiction bases are unavailable."
        rate_codes = set(
            TaxUnitRate.objects.filter(
                county="brazos", tax_year=projection.tax_year, tax_unit_code__in=unit_codes
            ).values_list("tax_unit_code", flat=True)
        )
        if rate_codes != unit_codes:
            return "One or more matching-year tax-unit rates are unavailable."
        has_assessment = AssessmentHistory.objects.filter(
            account_number=account.prop_id,
            tax_year=projection.tax_year,
            county="brazos",
            assessed_value__isnull=False,
        ).exists()
        if any(row.taxable_value is None for row in bases) and not has_assessment:
            return "Matching-year taxable or assessed value is unavailable."
        return None

    @staticmethod
    def _positive(value: Decimal | None) -> bool:
        return value is not None and value > 0

    @staticmethod
    def _land_area(account: PropertyAccount) -> Decimal | None:
        return PropertyLand.objects.filter(
            prop_id=account.prop_id, tax_year=account.tax_year
        ).aggregate(area=Sum("acreage"))["area"]

    @staticmethod
    def _comparison_facts(account: PropertyAccount) -> set[str]:
        facts: set[str] = set()
        if account.class_code.strip():
            facts.add("class")
        improvement = (
            PropertyImprovement.objects.filter(
                prop_id=account.prop_id, tax_year=account.tax_year, improvement_type="R"
            )
            .order_by("imp_id")
            .first()
        )
        if account.year_built or (improvement and improvement.year_built):
            facts.add("effective_age")
        if improvement is not None:
            building = PropertyBuildingCharacteristic.objects.filter(
                prop_id=account.prop_id, tax_year=account.tax_year, imp_id=improvement.imp_id
            ).first()
            if building is not None and (
                building.bedrooms is not None or building.bathrooms is not None
            ):
                facts.add("beds_baths")
        if BrazosActiveSnapshotReadiness._positive(
            BrazosActiveSnapshotReadiness._land_area(account)
        ):
            facts.add("land_area")
        if PropertyExtraFeature.objects.filter(
            prop_id=account.prop_id, tax_year=account.tax_year
        ).exists():
            facts.add("features")
        return facts
