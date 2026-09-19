"""The equity maths behind every county's protest report.

Two conclusions come out of a subject property and its comparables:

* an **equity summary** — how far the subject's $/sqft sits above the comparable
  median, and what correcting that gap would be worth (§41.43 "equal and
  uniform" is the same statute in every Texas county);
* a **protest recommendation** — a plain-language verdict for the comparables page.

Both are pure functions over :class:`~counties.common.contracts.Comp` records,
so a county gets them for free once its adapter can produce comps.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from counties.common.charts import (
    assessment_history_chart,
    ppsf_distribution_chart,
    score_breakdown_summary,
)
from counties.common.contracts import Comp, CountyAdapter, Subject
from counties.common.tax_evaluation import (
    evaluate_assessment_history,
    history_availability_notice,
)

ONE_HUNDRED = Decimal("100")
PERCENT = Decimal("0.01")

#: A recommendation needs at least this many comparables with usable $/sqft.
MIN_COMPS_FOR_RECOMMENDATION = 3


@dataclass(frozen=True)
class EquitySummary:
    """Where the subject sits relative to its comparables, on a $/sqft basis."""

    subject_value_per_sqft: float | None
    median_comp_value_per_sqft: float | None
    equity_gap_per_sqft: float | None
    estimated_savings: float | None
    comps_below_subject: int
    qualifying_ppsf: list[float] = field(default_factory=list)
    #: What the subject would be assessed at if it matched the comparable median.
    median_assessed_value: Decimal | None = None

    @property
    def qualifying_comp_count(self) -> int:
        return len(self.qualifying_ppsf)


def summarize_equity(subject: Subject, comps: Sequence[Comp]) -> EquitySummary:
    subject_ppsf = subject.value_per_sqft
    qualifying = [c.value_per_sqft for c in comps if c.value_per_sqft is not None]

    if subject_ppsf is None or not qualifying:
        return EquitySummary(
            subject_value_per_sqft=subject_ppsf,
            median_comp_value_per_sqft=None,
            equity_gap_per_sqft=None,
            estimated_savings=None,
            comps_below_subject=0,
            qualifying_ppsf=qualifying,
        )

    median_ppsf = statistics.median(qualifying)
    gap = subject_ppsf - median_ppsf
    area = subject.living_area
    savings = max(0.0, gap * float(area)) if area else None
    median_assessed = Decimal(str(median_ppsf)) * Decimal(str(float(area))) if area else None

    return EquitySummary(
        subject_value_per_sqft=subject_ppsf,
        median_comp_value_per_sqft=median_ppsf,
        equity_gap_per_sqft=gap,
        estimated_savings=savings,
        comps_below_subject=sum(1 for value in qualifying if value < subject_ppsf),
        qualifying_ppsf=qualifying,
        median_assessed_value=median_assessed,
    )


@dataclass(frozen=True)
class ProtestRecommendation:
    """Plain-language verdict shown on the comparables page."""

    level: str  # strong | moderate | neutral | low
    headline: str
    reason: str
    median: float
    average: float
    minimum: float
    maximum: float
    comparable_count: int
    average_score: float


def recommend_protest(
    subject_value_per_sqft: float | None, comps: Sequence[Comp]
) -> ProtestRecommendation | None:
    """Compare the subject's $/sqft against the comparable median.

    Returns ``None`` when there is not enough data to say anything useful — the
    page shows a "not enough data" note instead of a weak verdict.
    """
    if not subject_value_per_sqft:
        return None

    usable = [
        (c.value_per_sqft, c.similarity_score)
        for c in comps
        if c.value_per_sqft is not None and c.similarity_score is not None
    ]
    if len(usable) < MIN_COMPS_FOR_RECOMMENDATION:
        return None

    values = sorted(value for value, _ in usable)
    median = statistics.median(values)
    average_score = statistics.mean(score for _, score in usable)
    over_percentage = ((subject_value_per_sqft - median) / median) * 100.0

    above = (
        f"Your price per sqft (${subject_value_per_sqft:.2f}) is about "
        f"{abs(over_percentage):.0f}% {{direction}} the median (${median:.2f}) of "
        f"{len(values)} similar properties"
    )

    if over_percentage >= 20:
        level, headline = "strong", "Recommend protesting"
        reason = above.format(direction="above") + f" (avg match score {average_score:.0f})."
    elif over_percentage >= 10:
        level, headline = "moderate", "Consider protesting"
        reason = above.format(direction="above") + f" (avg match score {average_score:.0f})."
    elif over_percentage <= -10:
        level, headline = "low", "Protest not recommended"
        reason = above.format(direction="below") + "."
    else:
        level, headline = "neutral", "Borderline – depends on other factors"
        reason = (
            f"Your price per sqft (${subject_value_per_sqft:.2f}) is close to the median "
            f"(${median:.2f}) of {len(values)} similar properties."
        )

    return ProtestRecommendation(
        level=level,
        headline=headline,
        reason=reason,
        median=median,
        average=statistics.mean(values),
        minimum=values[0],
        maximum=values[-1],
        comparable_count=len(values),
        average_score=average_score,
    )


def sort_comps_for_display(comps: Sequence[Comp]) -> list[Comp]:
    """Best match first, then nearest, then cheapest per sqft.

    The one order every comp table uses. Both the Similar Properties page and
    the protest report call this on whatever ``adapter.find_comps()`` returns,
    so a homeowner comparing the two pages for the same property sees comps in
    the same relative order on both — a stable order that reads the way a
    homeowner scans the table.
    """
    return sorted(
        comps,
        key=lambda comp: (
            -float(comp.similarity_score or 0),
            comp.distance if comp.distance is not None else float("inf"),
            comp.value_per_sqft if comp.value_per_sqft is not None else float("inf"),
            comp.key or "",
        ),
    )


def percentile_of(value: float | None, population: Sequence[float]) -> float | None:
    """Where ``value`` falls within ``population``, as a 0-100 percentile."""
    if value is None or not population:
        return None
    at_or_below = sum(1 for candidate in population if candidate <= value)
    return (at_or_below / len(population)) * 100.0


def assessment_history_rows(
    account_number: str, county: str = "harris", limit: int = 5
) -> list[dict[str, Any]]:
    """Per-year assessed values, newest first, with YoY change and cap status."""
    return evaluate_assessment_history(county, account_number, limit=limit)


# --------------------------------------------------------------------------- comparables dossier

SIMILAR_DEFAULT_MAX_DISTANCE = 10.0
SIMILAR_MIN_MAX_DISTANCE = 0.1
SIMILAR_MAX_MAX_DISTANCE = 50.0
SIMILAR_DEFAULT_MAX_RESULTS = 20
SIMILAR_MIN_MAX_RESULTS = 1
SIMILAR_MAX_MAX_RESULTS = 100
SIMILAR_DEFAULT_MIN_SCORE = 30.0
SIMILAR_MIN_MIN_SCORE = 0.0
SIMILAR_MAX_MIN_SCORE = 100.0


def clamped_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(upper, parsed))


@dataclass(frozen=True)
class ComparablesDossier:
    """The comparables and similarity package for a subject property."""

    subject: Subject
    comps: Sequence[Comp]
    subject_percentile: float | None
    recommendation: ProtestRecommendation | None
    history: Sequence[Mapping[str, Any]]
    assessment_history_chart: Mapping[str, Any] | None
    max_distance: float
    max_results: int
    min_score: float


@dataclass(frozen=True)
class ComparablesDossierOutcome:
    """Polymorphic result of evaluating a comparables request."""

    status: Literal["ready", "unavailable"]
    dossier: ComparablesDossier | None = None
    subject: Subject | None = None
    error: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.status == "ready" and self.dossier is not None


def build_comparables_dossier(
    adapter: CountyAdapter,
    key: str,
    *,
    max_distance: Any = None,
    max_results: Any = None,
    min_score: Any = None,
) -> ComparablesDossierOutcome:
    subject = adapter.get_subject(key)
    if subject is None:
        return ComparablesDossierOutcome(
            status="unavailable", subject=None, error="Property not found"
        )

    caps = adapter.capabilities(key)
    if not caps.comparable_ready:
        return ComparablesDossierOutcome(
            status="unavailable",
            subject=subject,
            error=caps.reason_for("comparable")
            or "This property does not have location data required for similarity search.",
        )

    effective_max_distance = clamped_float(
        max_distance,
        SIMILAR_DEFAULT_MAX_DISTANCE,
        SIMILAR_MIN_MAX_DISTANCE,
        SIMILAR_MAX_MAX_DISTANCE,
    )
    effective_max_results = clamped_int(
        max_results,
        SIMILAR_DEFAULT_MAX_RESULTS,
        SIMILAR_MIN_MAX_RESULTS,
        SIMILAR_MAX_MAX_RESULTS,
    )
    effective_min_score = clamped_float(
        min_score,
        SIMILAR_DEFAULT_MIN_SCORE,
        SIMILAR_MIN_MIN_SCORE,
        SIMILAR_MAX_MIN_SCORE,
    )

    raw_comps = adapter.find_comps(
        key,
        max_distance_miles=effective_max_distance,
        max_results=effective_max_results,
        min_score=effective_min_score,
    )
    comps = sort_comps_for_display(raw_comps)

    subject_ppsf = subject.value_per_sqft
    population = [c.value_per_sqft for c in comps if c.value_per_sqft is not None]
    if subject_ppsf is not None:
        population.append(subject_ppsf)

    history = adapter.assessment_history(key)
    recommendation = recommend_protest(subject_ppsf, comps)

    dossier = ComparablesDossier(
        subject=subject,
        comps=comps,
        subject_percentile=percentile_of(subject_ppsf, population),
        recommendation=recommendation,
        history=history,
        assessment_history_chart=assessment_history_chart(history),
        max_distance=effective_max_distance,
        max_results=effective_max_results,
        min_score=effective_min_score,
    )
    return ComparablesDossierOutcome(status="ready", dossier=dossier, subject=subject)


# --------------------------------------------------------------------------- protest dossier

PROTEST_DEFAULT_MIN_SCORE = 70.0
PROTEST_MIN_MIN_SCORE = 52.0
PROTEST_MAX_MIN_SCORE = 100.0
PROTEST_MAX_COMPS = 50
PROTEST_MAX_DISTANCE = 10.0


def clamped_float(value: Any, default: float, lower: float, upper: float) -> float:
    """Parse and clamp numeric values to bounds, falling back to default."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(upper, parsed))


@dataclass(frozen=True)
class ProtestCompRow:
    """Formatted comparable property row for evidence tables and exports."""

    comp: Comp
    value_per_sqft: float | None
    delta: float | None
    breakdown_summary: str


@dataclass(frozen=True)
class ProtestEvidenceDossier:
    """The complete evidence package required for an ARB hearing."""

    subject: Subject
    comps: Sequence[Comp]
    equity: EquitySummary
    history: Sequence[Mapping[str, Any]]
    history_notice: str
    tax_impact: Any
    comp_rows: Sequence[ProtestCompRow]
    min_score: float
    assessment_history_chart: Mapping[str, Any] | None
    ppsf_distribution_chart: Mapping[str, Any] | None


@dataclass(frozen=True)
class ProtestDossierOutcome:
    """Polymorphic result of evaluating a protest evidence dossier request."""

    status: Literal["ready", "unavailable"]
    dossier: ProtestEvidenceDossier | None = None
    subject: Subject | None = None
    error: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.status == "ready" and self.dossier is not None


def build_protest_dossier(
    adapter: CountyAdapter,
    key: str,
    *,
    min_score: Any = None,
) -> ProtestDossierOutcome:
    """Prepare a full protest evidence dossier across the county adapter seam."""
    subject = adapter.get_subject(key)
    if subject is None:
        return ProtestDossierOutcome(status="unavailable", subject=None, error="Property not found")

    caps = adapter.capabilities(key)
    if not caps.report_ready:
        return ProtestDossierOutcome(
            status="unavailable",
            subject=subject,
            error=caps.reason_for("report")
            or "This property does not have location data required for similarity search.",
        )

    if not subject.has_location:
        return ProtestDossierOutcome(
            status="unavailable",
            subject=subject,
            error="This property does not have location data required for similarity search.",
        )

    effective_min_score = clamped_float(
        min_score,
        PROTEST_DEFAULT_MIN_SCORE,
        PROTEST_MIN_MIN_SCORE,
        PROTEST_MAX_MIN_SCORE,
    )
    raw_comps = adapter.find_comps(
        subject.key,
        max_distance_miles=PROTEST_MAX_DISTANCE,
        max_results=PROTEST_MAX_COMPS,
        min_score=effective_min_score,
    )
    comps = sort_comps_for_display(raw_comps)
    equity = summarize_equity(subject, comps)
    history = adapter.assessment_history(subject.key)
    history_notice = history_availability_notice(history, subject.tax_year)
    tax_impact = adapter.tax_impact(subject.key, subject.tax_year, equity.median_assessed_value)

    comp_rows = [
        ProtestCompRow(
            comp=comp,
            value_per_sqft=comp.value_per_sqft,
            delta=comp.delta_vs(equity.subject_value_per_sqft),
            breakdown_summary=score_breakdown_summary(comp.score_breakdown),
        )
        for comp in comps
    ]

    dossier = ProtestEvidenceDossier(
        subject=subject,
        comps=comps,
        equity=equity,
        history=history,
        history_notice=history_notice,
        tax_impact=tax_impact,
        comp_rows=comp_rows,
        min_score=effective_min_score,
        assessment_history_chart=assessment_history_chart(history),
        ppsf_distribution_chart=ppsf_distribution_chart(
            equity.qualifying_ppsf, equity.subject_value_per_sqft
        ),
    )
    return ProtestDossierOutcome(status="ready", dossier=dossier, subject=subject)
