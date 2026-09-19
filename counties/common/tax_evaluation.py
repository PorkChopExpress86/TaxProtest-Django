"""Texas statutory property-tax assessment history and tax impact evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from counties.common.cap_status import evaluate_cap_status
from counties.common.tax_impact import TaxImpactResult, calculate_tax_impact
from counties.common.tax_models import AssessmentHistory

ONE_HUNDRED = Decimal("100")
PERCENT = Decimal("0.01")


def year_over_year_percent(current: Any, prior: Any) -> Decimal | None:
    """Percentage change between two assessed values, rounded to two places."""
    if current is None or not prior:
        return None
    return ((Decimal(current) - Decimal(prior)) / Decimal(prior) * ONE_HUNDRED).quantize(
        PERCENT, rounding=ROUND_HALF_UP
    )


def history_availability_notice(
    history: Sequence[Mapping[str, Any]], source_year: int | None
) -> str:
    years = {row["tax_year"] for row in history if row.get("assessed_value") is not None}
    if not years:
        return "Assessment history unavailable. Qualified property evidence remains available."
    latest = source_year or max(years)
    gaps = sorted(set(range(max(min(years), latest - 4), latest + 1)) - years)
    return "Assessment history gaps: " + ", ".join(map(str, gaps)) if gaps else ""


def evaluate_assessment_history(
    county: str,
    account_number: str,
    *,
    limit: int = 5,
    has_typed_cap_flag: bool | None = None,
) -> list[dict[str, Any]]:
    """Per-year assessed values, newest first, with YoY change and statutory cap status."""
    history = list(
        AssessmentHistory.objects.filter(account_number=account_number, county=county).order_by(
            "-tax_year"
        )[:limit]
    )

    rows = []
    for index, entry in enumerate(history):
        prior = history[index + 1] if index + 1 < len(history) else None
        if prior is not None and prior.tax_year != entry.tax_year - 1:
            prior = None
        rows.append(
            {
                "tax_year": entry.tax_year,
                "assessed_value": entry.assessed_value,
                "appraised_value": entry.appraised_value,
                "market_value": entry.market_value,
                "increase_percent": year_over_year_percent(
                    entry.assessed_value, prior.assessed_value if prior else None
                ),
                "cap_status": evaluate_cap_status(
                    entry, prior, has_typed_cap_flag=has_typed_cap_flag
                ),
            }
        )
    return rows


def evaluate_tax_impact(
    county: str,
    account_number: str,
    tax_year: int | None,
    target_assessed_value: Decimal | float | None,
) -> TaxImpactResult:
    """Calculate tax liability scenarios under Texas Tax Code rules."""
    return calculate_tax_impact(
        account_number=account_number,
        tax_year=tax_year,
        median_assessed_value=target_assessed_value,
        county=county,
    )


__all__ = [
    "TaxImpactResult",
    "evaluate_assessment_history",
    "evaluate_tax_impact",
    "history_availability_notice",
    "year_over_year_percent",
]
