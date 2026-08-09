"""Assessment-history rows for the shared report, from the shared table.

``AssessmentHistory`` is county-scoped rather than county-owned (it lives in the
Harris app for historical reasons but carries a ``county`` column), so both
counties read it through this one helper and get identically shaped rows.
"""

from __future__ import annotations

from typing import Any

from counties.common.analysis import year_over_year_percent
from counties.common.cap_status import evaluate_cap_status


def assessment_history_rows(
    account_number: str, county: str = "harris", limit: int = 5
) -> list[dict[str, Any]]:
    """Per-year assessed values, newest first, with YoY change and cap status."""
    # Imported lazily: AssessmentHistory is a Harris-app model shared via its
    # county column (wayfinder ticket #9); importing a county app at module
    # scope would invert the county-neutral layer's dependency direction.
    # evaluate_cap_status is genuinely part of this layer -- see cap_status.py.
    from counties.harris.models import AssessmentHistory

    history = list(
        AssessmentHistory.objects.filter(account_number=account_number, county=county).order_by(
            "-tax_year"
        )[:limit]
    )

    rows = []
    for index, entry in enumerate(history):
        prior = history[index + 1] if index + 1 < len(history) else None
        rows.append(
            {
                "tax_year": entry.tax_year,
                "assessed_value": entry.assessed_value,
                "appraised_value": entry.appraised_value,
                "market_value": entry.market_value,
                "increase_percent": year_over_year_percent(
                    entry.assessed_value, prior.assessed_value if prior else None
                ),
                "cap_status": evaluate_cap_status(entry, prior),
            }
        )
    return rows
