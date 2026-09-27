"""Harris readiness projection: one source-backed rule for coverage and the web.

The rule judges one record's facts against dataset facts. Coverage qualification
drives it in bulk over every record; the Harris adapter drives it for one property
with bounded queries. Both answer from the same rule, so what the web offers is
exactly what publication coverage measured.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass

from django.db.models import QuerySet

from counties.common.analysis import MIN_COMPS_FOR_RECOMMENDATION
from counties.common.import_coverage import OutcomePopulation
from counties.common.models import ImportCandidate
from counties.common.tax_models import PropertyJurisdictionExemption, TaxUnitRate
from counties.harris.models import BuildingDetail, PropertyRecord

OUTCOMES = ("search", "comparable", "report", "tax")
_POOL_SIZE = MIN_COMPS_FOR_RECOMMENDATION + 1
_CHUNK = 10000
_RECORD_FIELDS = (
    "account_number",
    "is_residential",
    "is_data_ready",
    "latitude",
    "longitude",
    "building_area",
    "assessed_value",
    "value",
)

NOT_RESIDENTIAL = "Not residential under Harris source classification"
NO_COORDINATES = "Coordinates unavailable"
NO_ROOM_FACTS = "Active bedroom/bathroom facts unavailable"
READINESS_INCOMPLETE = "Harris data readiness incomplete"
NO_LOCATION = "This property does not have location data required for similarity search."
NO_EQUITY_INPUTS = "Positive assessed value, living area and coordinates are required"
POOL_TOO_SMALL = "At least three qualifying comparables are required"
YEAR_NOT_RECORDED = (
    "Published property source year is not recorded; matching-year tax impact is unavailable."
)
NO_TAX_INPUTS = "Matching-year report, jurisdiction and rate prerequisites unavailable"
NO_EXEMPTIONS = "Matching-year jurisdiction and exemption rows are unavailable"


def published_year() -> int | None:
    """The source year recorded by the published Harris candidate, if any."""
    return (
        ImportCandidate.objects.filter(county="harris", state="published")
        .values_list("evidence__property_source_year", flat=True)
        .first()
    )


@dataclass(frozen=True, slots=True)
class HarrisRecordFacts:
    """The source facts of one Harris record that readiness depends on."""

    account_number: str
    is_residential: bool
    is_data_ready: bool
    has_coordinates: bool
    has_room_facts: bool
    has_equity_inputs: bool

    @property
    def search_ready(self) -> bool:
        return self.is_residential and self.is_data_ready

    @property
    def in_report_pool(self) -> bool:
        return self.search_ready and self.has_coordinates and self.has_equity_inputs


@dataclass(frozen=True)
class HarrisDatasetFacts:
    """Facts about the published dataset that one record's readiness depends on."""

    year: int | None
    equity_supported: bool
    pool_supported: bool
    tax_inputs: bool


@dataclass(frozen=True)
class HarrisReadiness:
    """Per-outcome readiness of one Harris record, with reasons when not ready."""

    ready: frozenset[str]
    reasons: Mapping[str, tuple[str, ...]]


def readiness(
    record: HarrisRecordFacts, dataset: HarrisDatasetFacts, *, has_exemptions: bool
) -> HarrisReadiness:
    """The one Harris readiness rule."""
    base: list[str] = []
    if not record.is_residential:
        base.append(NOT_RESIDENTIAL)
    if not record.is_data_ready:
        if not record.has_coordinates:
            base.append(NO_COORDINATES)
        if not record.has_room_facts:
            base.append(NO_ROOM_FACTS)
        if not base:
            base.append(READINESS_INCOMPLETE)
    if base:
        return HarrisReadiness(frozenset(), {outcome: tuple(base) for outcome in OUTCOMES})

    reasons: dict[str, tuple[str, ...]] = {}
    if not record.has_coordinates:
        reasons["comparable"] = reasons["report"] = (NO_LOCATION,)
    elif not record.has_equity_inputs:
        reasons["report"] = (NO_EQUITY_INPUTS,)
    elif not dataset.pool_supported:
        reasons["report"] = (POOL_TOO_SMALL,)
    if "report" in reasons:
        reasons["tax"] = reasons["report"]
    elif dataset.year is None:
        reasons["tax"] = (YEAR_NOT_RECORDED,)
    elif not dataset.tax_inputs:
        reasons["tax"] = (NO_TAX_INPUTS,)
    elif not has_exemptions:
        reasons["tax"] = (NO_EXEMPTIONS,)
    return HarrisReadiness(
        frozenset(outcome for outcome in OUTCOMES if outcome not in reasons), reasons
    )


def _record_facts(
    rows: Iterable[tuple], *, chunk_size: int = _CHUNK
) -> Iterator[HarrisRecordFacts]:
    """Translate record rows plus each account's first active building into facts."""
    chunk: list[tuple] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) == chunk_size:
            yield from _facts_for_chunk(chunk)
            chunk = []
    if chunk:
        yield from _facts_for_chunk(chunk)


def _facts_for_chunk(rows: list[tuple]) -> Iterator[HarrisRecordFacts]:
    buildings: dict[str, tuple] = {}
    for account, heat_area, bedrooms, bathrooms in (
        BuildingDetail.objects.filter(is_active=True, account_number__in={row[0] for row in rows})
        .order_by("id")
        .values_list("account_number", "heat_area", "bedrooms", "bathrooms")
    ):
        buildings.setdefault(account, (heat_area, bedrooms, bathrooms))
    for key, residential, data_ready, latitude, longitude, area, assessed, value in rows:
        building = buildings.get(key)
        living_area = building[0] if building and building[0] else area
        appraised = assessed or value
        yield HarrisRecordFacts(
            account_number=key,
            is_residential=residential,
            is_data_ready=data_ready,
            has_coordinates=latitude is not None and longitude is not None,
            has_room_facts=building is not None
            and building[1] is not None
            and building[2] is not None,
            has_equity_inputs=bool(living_area and living_area > 0 and appraised and appraised > 0),
        )


def _tax_inputs(year: int | None, equity_supported: bool) -> bool:
    return bool(
        year
        and equity_supported
        and TaxUnitRate.objects.filter(county="harris", tax_year=year).exists()
        and PropertyJurisdictionExemption.objects.filter(county="harris", tax_year=year).exists()
    )


def _pool_reached(rows: QuerySet, predicate: Callable[[HarrisRecordFacts], bool]) -> bool:
    """Whether enough records satisfy ``predicate``, reading only until they do."""
    found = 0
    for record in _record_facts(rows.iterator(chunk_size=_POOL_SIZE), chunk_size=_POOL_SIZE):
        if predicate(record):
            found += 1
            if found >= _POOL_SIZE:
                return True
    return False


class HarrisReadinessProjection:
    """Resolve the readiness of one published Harris property with bounded queries."""

    def dataset_facts(self) -> HarrisDatasetFacts:
        year = published_year()
        records = PropertyRecord.objects.order_by("id").values_list(*_RECORD_FIELDS)
        equity_supported = _pool_reached(records, lambda record: record.has_equity_inputs)
        pool_supported = _pool_reached(
            records.filter(
                is_residential=True,
                is_data_ready=True,
                latitude__isnull=False,
                longitude__isnull=False,
            ),
            lambda record: record.in_report_pool,
        )
        return HarrisDatasetFacts(
            year=year,
            equity_supported=equity_supported,
            pool_supported=pool_supported,
            tax_inputs=_tax_inputs(year, equity_supported),
        )

    def project(self, account_number: str) -> HarrisReadiness | None:
        row = (
            PropertyRecord.objects.filter(account_number=account_number)
            .order_by("-is_residential", "-is_data_ready", "id")
            .values_list(*_RECORD_FIELDS)
            .first()
        )
        if row is None:
            return None
        (record,) = _facts_for_chunk([row])
        dataset = self.dataset_facts()
        has_exemptions = bool(
            dataset.year
            and PropertyJurisdictionExemption.objects.filter(
                county="harris", tax_year=dataset.year, account_number=account_number
            ).exists()
        )
        return readiness(record, dataset, has_exemptions=has_exemptions)


def outcome_populations(
    year: int | None, *, claimed_gis: bool = False
) -> dict[str, OutcomePopulation]:
    """Coverage outcome populations: the readiness rule applied to every record."""
    records = list(
        _record_facts(
            PropertyRecord.objects.order_by("id")
            .values_list(*_RECORD_FIELDS)
            .iterator(chunk_size=_CHUNK)
        )
    )
    equity_supported = sum(record.has_equity_inputs for record in records) >= _POOL_SIZE
    dataset = HarrisDatasetFacts(
        year=year,
        equity_supported=equity_supported,
        pool_supported=sum(record.in_report_pool for record in records) >= _POOL_SIZE,
        tax_inputs=_tax_inputs(year, equity_supported),
    )
    exempt = (
        set(
            PropertyJurisdictionExemption.objects.filter(county="harris", tax_year=year)
            .values_list("account_number", flat=True)
            .distinct()
        )
        if dataset.tax_inputs
        else set()
    )
    eligible: dict[str, set[str]] = {outcome: set() for outcome in OUTCOMES}
    exclusions: dict[str, dict[str, list[str]]] = {outcome: {} for outcome in OUTCOMES}
    for record in records:
        result = readiness(record, dataset, has_exemptions=record.account_number in exempt)
        for outcome in OUTCOMES:
            if outcome in result.ready:
                eligible[outcome].add(record.account_number)
            else:
                exclusions[outcome][record.account_number] = list(result.reasons[outcome])
    comparable_supported = claimed_gis or any(record.has_coordinates for record in records)
    tax_supported = bool(eligible["tax"])
    return {
        "search": OutcomePopulation(eligible["search"], exclusions=exclusions["search"]),
        "comparable": OutcomePopulation(
            eligible["comparable"],
            supported=comparable_supported,
            reason="" if comparable_supported else "GIS coordinates unavailable",
            exclusions=exclusions["comparable"],
        ),
        "report": OutcomePopulation(
            eligible["report"],
            supported=dataset.equity_supported,
            reason=(
                ""
                if dataset.equity_supported
                else "A qualifying equity comparison population is unavailable"
            ),
            exclusions=exclusions["report"],
        ),
        "tax": OutcomePopulation(
            eligible["tax"],
            supported=tax_supported,
            reason="" if tax_supported else NO_TAX_INPUTS,
            exclusions=exclusions["tax"],
        ),
    }
