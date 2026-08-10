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
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from counties.common.contracts import Comp, Subject

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


def year_over_year_percent(current, prior) -> Decimal | None:
    """Percentage change between two assessed values, rounded to two places."""
    if current is None or not prior:
        return None
    return ((Decimal(current) - Decimal(prior)) / Decimal(prior) * ONE_HUNDRED).quantize(
        PERCENT, rounding=ROUND_HALF_UP
    )
