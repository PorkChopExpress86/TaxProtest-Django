"""Assessment-history rows for the shared report, from the shared table.

``AssessmentHistory`` is county-scoped rather than county-owned (its
``Meta.app_label`` is pinned to Harris's "data" label for migration-history
continuity, but the model itself lives in ``counties.common.tax_models`` --
see that module's docstring), so both counties read it through this one
helper and get identically shaped rows.
"""

from __future__ import annotations

from typing import Any

from counties.common.analysis import year_over_year_percent
from counties.common.cap_status import evaluate_cap_status
from counties.common.tax_models import AssessmentHistory


def history_availability_notice(history, source_year):
    years = {row["tax_year"] for row in history if row.get("assessed_value") is not None}
    if not years:
        return "Assessment history unavailable. Qualified property evidence remains available."
    latest = source_year or max(years)
    gaps = sorted(set(range(max(min(years), latest - 4), latest + 1)) - years)
    return "Assessment history gaps: " + ", ".join(map(str, gaps)) if gaps else ""


def assessment_history_rows(
    account_number: str, county: str = "harris", limit: int = 5
) -> list[dict[str, Any]]:
    """Per-year assessed values, newest first, with YoY change and cap status."""
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
                "cap_status": evaluate_cap_status(entry, prior),
            }
        )
    return rows
