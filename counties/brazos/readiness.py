"""The county-owned active-snapshot and readiness read module for Brazos."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import NamedTuple

from django.db.models import Case, Exists, IntegerField, OuterRef, Q, Subquery, Sum, Value, When
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
from counties.brazos.tax_units import NON_LEVYING_UNITS
from counties.common.analysis import MIN_COMPS_FOR_RECOMMENDATION, assessment_history_rows
from counties.common.tax_impact import taxing_units
from counties.common.tax_models import AssessmentHistory, PropertyJurisdictionExemption, TaxUnitRate

_CHUNK = 2000
# The subject plus at least three other same-mode report-pool members (ADR-0020).
REPORT_POOL_SIZE = MIN_COMPS_FOR_RECOMMENDATION + 1

NO_EQUITY_FACTS = "Positive assessed value and living area are required for a report."
POOL_TOO_SMALL = (
    "At least three same-mode comparable-ready properties with equity facts are required."
)


@dataclass(frozen=True)
class BrazosReadinessProjection:
    """Immutable readiness facts for one property in the active snapshot."""

    snapshot_id: int
    tax_year: int
    prop_id: str
    owner_name: str
    search_ready: bool
    comparable_ready: bool
    has_equity_facts: bool
    report_ready: bool
    tax_impact_ready: bool
    comparison_mode: str | None
    coordinate_source: str
    coordinate_source_year: int | None
    reasons: tuple[tuple[str, str], ...]

    def reason_for(self, capability: str) -> str | None:
        return dict(self.reasons).get(capability)

    @property
    def in_report_pool(self) -> bool:
        """Comparable-ready with positive assessed value and living area."""
        return self.comparable_ready and self.has_equity_facts


@dataclass(frozen=True)
class BrazosSnapshotFacts:
    """Facts about the active snapshot that one property's report readiness depends on.

    ``reached_modes`` names the comparison modes whose report pool holds a subject plus
    three other members. A one-property reading checks only that property's own mode.
    """

    reached_modes: frozenset[str]

    @classmethod
    def surveyed(cls, records: Iterable[BrazosReadinessProjection]) -> BrazosSnapshotFacts:
        """The report pool counted over every property of one snapshot."""
        pool = Counter(record.comparison_mode for record in records if record.in_report_pool)
        return cls(
            reached_modes=frozenset(
                mode for mode, count in pool.items() if mode and count >= REPORT_POOL_SIZE
            )
        )

    @property
    def report_pool_reached(self) -> bool:
        """Whether any comparison mode holds a subject plus three other pool members."""
        return bool(self.reached_modes)


def report_reason(record: BrazosReadinessProjection, snapshot: BrazosSnapshotFacts) -> str | None:
    """Why one record is not report-ready, judged with no distance or similarity score."""
    if not record.comparable_ready:
        return record.reason_for("comparable")
    if not record.has_equity_facts:
        return NO_EQUITY_FACTS
    if record.comparison_mode not in snapshot.reached_modes:
        return POOL_TOO_SMALL
    return None


def readiness(
    record: BrazosReadinessProjection, snapshot: BrazosSnapshotFacts, *, tax_gap: str | None
) -> BrazosReadinessProjection:
    """The one Brazos readiness rule over a record's facts and its snapshot's facts.

    ``tax_gap`` names the record's missing matching-year tax inputs, if any; it is
    consulted only for a report-ready record.
    """
    report = report_reason(record, snapshot)
    tax = report or tax_gap
    reasons = dict(record.reasons)
    for capability, reason in (("report", report), ("tax", tax)):
        if reason:
            reasons[capability] = reason
        else:
            reasons.pop(capability, None)
    return replace(
        record,
        report_ready=report is None,
        tax_impact_ready=tax is None,
        reasons=tuple(reasons.items()),
    )


class BrazosActiveSnapshotReadiness:
    """Resolve active Brazos records and their source-backed capabilities.

    It keeps no memo or cache: candidate measurement switches the database search
    path, and the adapter holding it is a long-lived singleton.
    """

    def active_snapshot(self) -> BrazosPropertySnapshot | None:
        return BrazosPropertySnapshot.objects.filter(is_active=True).first()

    def project(self, prop_id: str) -> BrazosReadinessProjection | None:
        """One property's readiness under the same rule ``survey`` applies to every property."""
        snapshot = self.active_snapshot()
        if snapshot is None:
            return None
        account = self.account(prop_id, snapshot=snapshot)
        if account is None:
            return None
        (record,) = self._project_chunk(snapshot, [account])
        reached: frozenset[str] = frozenset()
        if (
            record.in_report_pool
            and record.comparison_mode
            and self._report_pool_count(snapshot, record.comparison_mode) >= REPORT_POOL_SIZE
        ):
            reached = frozenset({record.comparison_mode})
        facts = BrazosSnapshotFacts(reached_modes=reached)
        tax_gap = None
        if report_reason(record, facts) is None:
            tax_gap = self.tax_input_gaps([prop_id], tax_year=snapshot.tax_year)[prop_id]
        return readiness(record, facts, tax_gap=tax_gap)

    def survey(
        self, *, snapshot: BrazosPropertySnapshot | None = None, chunk_size: int = _CHUNK
    ) -> dict[str, BrazosReadinessProjection]:
        """Every active-snapshot property's readiness, a fixed query count per chunk."""
        snapshot = snapshot or self.active_snapshot()
        if snapshot is None:
            return {}
        records = list(
            self.static_projections(
                PropertyAccount.objects.filter(tax_year=snapshot.tax_year)
                .order_by("pk")
                .iterator(chunk_size=chunk_size),
                snapshot=snapshot,
                chunk_size=chunk_size,
            )
        )
        facts = BrazosSnapshotFacts.surveyed(records)
        gaps = self.tax_input_gaps(
            [record.prop_id for record in records if report_reason(record, facts) is None],
            tax_year=snapshot.tax_year,
            chunk_size=chunk_size,
        )
        return {
            record.prop_id: readiness(record, facts, tax_gap=gaps.get(record.prop_id))
            for record in records
        }

    def _report_pool_count(self, snapshot: BrazosPropertySnapshot, mode: str) -> int:
        """Same-mode report-pool members, up to ``REPORT_POOL_SIZE``, in one query.

        The SQL restates ``_project_chunk`` and ``_projection`` for report-pool members;
        the one-property and survey parity tests hold the two to one answer.
        """
        year = snapshot.tax_year
        first_improvement = PropertyImprovement.objects.filter(
            prop_id=OuterRef("prop_id"), tax_year=year, improvement_type="R"
        ).order_by("imp_id")
        members = (
            PropertyAccount.objects.filter(
                tax_year=year,
                assessed_value__gt=0,
                living_area__gt=0,
                latitude__isnull=False,
                longitude__isnull=False,
                coordinate_source_year__isnull=False,
            )
            .exclude(coordinate_source="")
            .annotate(
                _imp_id=Subquery(first_improvement.values("imp_id")[:1]),
                _imp_year_built=Subquery(first_improvement.values("year_built")[:1]),
                _land_area=Subquery(
                    PropertyLand.objects.filter(prop_id=OuterRef("prop_id"), tax_year=year)
                    .values("prop_id")
                    .annotate(area=Sum("acreage"))
                    .values("area")
                ),
            )
            .annotate(
                _facts=_fact(Q(class_code__regex=r"\S"))
                + _fact(Q(year_built__gt=0) | Q(year_built__lt=0) | Q(_imp_year_built__gt=0))
                + _fact(
                    Exists(
                        PropertyBuildingCharacteristic.objects.filter(
                            Q(bedrooms__isnull=False) | Q(bathrooms__isnull=False),
                            prop_id=OuterRef("prop_id"),
                            tax_year=year,
                            imp_id=OuterRef("_imp_id"),
                        )
                    )
                )
                + _fact(Q(_land_area__gt=0))
                + _fact(
                    Exists(
                        PropertyExtraFeature.objects.filter(
                            prop_id=OuterRef("prop_id"), tax_year=year
                        )
                    )
                )
            )
        )
        if mode == "residential":
            members = members.filter(_facts__gte=2)
        else:
            members = members.filter(_facts__lt=2, _land_area__gt=0)
        return members.values("pk")[:REPORT_POOL_SIZE].count()

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
        (projection,) = self._project_chunk(snapshot, [account])
        return projection

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

    def static_projections(
        self,
        accounts: Iterable[PropertyAccount],
        *,
        snapshot: BrazosPropertySnapshot | None = None,
        chunk_size: int = _CHUNK,
    ) -> Iterator[BrazosReadinessProjection]:
        """Project per-record facts for many accounts, in order, a fixed query count per chunk."""
        snapshot = snapshot or self.active_snapshot()
        if snapshot is None:
            return
        chunk: list[PropertyAccount] = []
        for account in accounts:
            chunk.append(account)
            if len(chunk) == chunk_size:
                yield from self._project_chunk(snapshot, chunk)
                chunk = []
        if chunk:
            yield from self._project_chunk(snapshot, chunk)

    def _project_chunk(
        self, snapshot: BrazosPropertySnapshot, accounts: list[PropertyAccount]
    ) -> Iterator[BrazosReadinessProjection]:
        year = snapshot.tax_year
        prop_ids = {account.prop_id for account in accounts}
        # Keep the immutable projection and the queryset on the same trimmed
        # predicate. This avoids returning whitespace-only names or addresses
        # from search while marking them unavailable to the subject surface.
        searchable = set(
            self.search_queryset(snapshot=snapshot)
            .filter(pk__in=[account.pk for account in accounts])
            .values_list("pk", flat=True)
        )
        land_areas = dict(
            PropertyLand.objects.filter(prop_id__in=prop_ids, tax_year=year)
            .values("prop_id")
            .annotate(area=Sum("acreage"))
            .values_list("prop_id", "area")
        )
        # The first residential improvement by imp_id, ordered by the database.
        improvements: dict[str, tuple[str, int | None]] = {}
        for prop_id, imp_id, year_built in (
            PropertyImprovement.objects.filter(
                prop_id__in=prop_ids, tax_year=year, improvement_type="R"
            )
            .order_by("prop_id", "imp_id")
            .values_list("prop_id", "imp_id", "year_built")
        ):
            improvements.setdefault(prop_id, (imp_id, year_built))
        # imp_id repeats across properties: building facts pair by both keys.
        room_facts = {
            (prop_id, imp_id): bedrooms is not None or bathrooms is not None
            for prop_id, imp_id, bedrooms, bathrooms in (
                PropertyBuildingCharacteristic.objects.filter(
                    prop_id__in=prop_ids, tax_year=year
                ).values_list("prop_id", "imp_id", "bedrooms", "bathrooms")
            )
        }
        featured = set(
            PropertyExtraFeature.objects.filter(prop_id__in=prop_ids, tax_year=year)
            .values_list("prop_id", flat=True)
            .distinct()
        )
        for account in accounts:
            improvement = improvements.get(account.prop_id)
            land_area = land_areas.get(account.prop_id)
            facts: set[str] = set()
            if account.class_code.strip():
                facts.add("class")
            if account.year_built or (improvement and improvement[1]):
                facts.add("effective_age")
            if improvement is not None and room_facts.get((account.prop_id, improvement[0])):
                facts.add("beds_baths")
            if self._positive(land_area):
                facts.add("land_area")
            if account.prop_id in featured:
                facts.add("features")
            yield self._projection(
                snapshot,
                account,
                search_ready=account.pk in searchable,
                land_area=land_area,
                facts=facts,
            )

    def _projection(
        self,
        snapshot: BrazosPropertySnapshot,
        account: PropertyAccount,
        *,
        search_ready: bool,
        land_area: Decimal | None,
        facts: set[str],
    ) -> BrazosReadinessProjection:
        reasons: dict[str, str] = {}
        if not search_ready:
            reasons["search"] = "The active property record has no owner or address."

        has_coordinates = bool(
            account.latitude is not None
            and account.longitude is not None
            and account.coordinate_source
            and account.coordinate_source_year is not None
        )
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
            has_equity_facts=self._positive(account.assessed_value)
            and self._positive(account.living_area),
            report_ready=False,
            tax_impact_ready=False,
            comparison_mode=mode,
            coordinate_source=account.coordinate_source,
            coordinate_source_year=account.coordinate_source_year,
            reasons=tuple(reasons.items()),
        )

    @classmethod
    def tax_input_gaps(
        cls, prop_ids: Iterable[str], *, tax_year: int, chunk_size: int = _CHUNK
    ) -> dict[str, str | None]:
        """Name each property's missing matching-year tax inputs, a fixed query count per chunk.

        This judges tax inputs only; tax-impact-ready also requires report-ready.
        """
        gaps: dict[str, str | None] = {}
        chunk: list[str] = []
        for prop_id in prop_ids:
            chunk.append(prop_id)
            if len(chunk) == chunk_size:
                gaps.update(cls._tax_input_gaps_chunk(chunk, tax_year))
                chunk = []
        if chunk:
            gaps.update(cls._tax_input_gaps_chunk(chunk, tax_year))
        return gaps

    @classmethod
    def _tax_input_gaps_chunk(cls, prop_ids: list[str], year: int) -> dict[str, str | None]:
        rows_by_prop: dict[str, list[_TaxRow]] = {prop_id: [] for prop_id in prop_ids}
        # Explicit order: a unit's name comes from its base row ("" sorts first),
        # else from its first named exemption row.
        for account_number, *row in (
            PropertyJurisdictionExemption.objects.filter(
                account_number__in=prop_ids, tax_year=year, county="brazos"
            )
            .order_by("account_number", "tax_unit_code", "exemption_code")
            .values_list(
                "account_number",
                "tax_unit_code",
                "tax_unit_name",
                "exemption_code",
                "exemption_amount",
                "exemption_percent",
                "taxable_value",
            )
        ):
            rows_by_prop[account_number].append(_TaxRow(*row))
        levying = {
            row.tax_unit_code for rows in rows_by_prop.values() for row in rows if row.tax_unit_code
        } - NON_LEVYING_UNITS.keys()
        rated = set(
            TaxUnitRate.objects.filter(
                county="brazos", tax_year=year, tax_unit_code__in=levying
            ).values_list("tax_unit_code", flat=True)
        )
        assessed = set(
            AssessmentHistory.objects.filter(
                account_number__in=prop_ids,
                tax_year=year,
                county="brazos",
                assessed_value__isnull=False,
            ).values_list("account_number", flat=True)
        )
        return {
            prop_id: _tax_input_gap(
                year, rows_by_prop[prop_id], rated=rated, has_assessment=prop_id in assessed
            )
            for prop_id in prop_ids
        }

    @staticmethod
    def _positive(value: Decimal | None) -> bool:
        return value is not None and value > 0


def _fact(condition: Q | Exists) -> Case:
    """One comparable fact as 1 or 0, for counting facts in SQL."""
    return Case(When(condition, then=Value(1)), default=Value(0), output_field=IntegerField())


class _TaxRow(NamedTuple):
    tax_unit_code: str
    tax_unit_name: str
    exemption_code: str
    exemption_amount: Decimal | None
    exemption_percent: Decimal | None
    taxable_value: Decimal | None


def _tax_input_gap(
    year: int, rows: list[_TaxRow], *, rated: set[str], has_assessment: bool
) -> str | None:
    """Name one property's missing tax inputs from its rows, ordered by unit and exemption."""
    names: dict[str, str] = {}
    for row in rows:
        if row.tax_unit_code:
            names[row.tax_unit_code] = names.get(row.tax_unit_code) or row.tax_unit_name
    if not names:
        return f"No {year} jurisdiction and exemption rows for this property"

    def units(codes: set[str]) -> str:
        return taxing_units({code: names[code] for code in codes})

    gaps = []
    bases = [row for row in rows if not row.exemption_code]
    unverified = {
        row.tax_unit_code
        for row in rows
        if row.exemption_code and row.exemption_amount is None and row.exemption_percent is None
    }
    if unverified:
        gaps.append(f"{year} exemption amounts are unverified for {units(unverified)}")
    unbased = names.keys() - {row.tax_unit_code for row in bases}
    if unbased:
        gaps.append(f"{year} gross jurisdiction base unavailable for {units(unbased)}")
    # Units that levy no tax need no rate (ADR-0019).
    unrated = names.keys() - NON_LEVYING_UNITS.keys() - rated
    if unrated:
        gaps.append(f"Adopted {year} rate unavailable for {units(unrated)}")
    if any(row.taxable_value is None for row in bases) and not has_assessment:
        gaps.append(f"{year} taxable or assessed value unavailable")
    return "; ".join(gaps) or None
