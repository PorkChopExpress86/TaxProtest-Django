from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from data.models import AssessmentHistory

CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")
ONE_HUNDRED = Decimal("100")
HOMESTEAD_LIMIT_PERCENT = Decimal("10")
CIRCUIT_BREAKER_LIMIT_PERCENT = Decimal("20")


@dataclass
class CapGapResult:
    """How the Texas appraised-value cap (Tax Code 23.23 / circuit breaker) interacts
    with a market-value protest for one property-year.

    ``scenario`` values:
      - ``missing``       — not enough value data to analyze
      - ``no_target``     — gap computed, but no comparable-implied target value exists
      - ``already_below`` — comps suggest a value at/above current market; protest unlikely to help
      - ``direct``        — no cap gap: a reduction to the target lowers this year's taxable basis
      - ``effective``     — cap engaged, but the target is below the capped value, so the
                            protest still lowers this year's taxable basis
      - ``blocked``       — cap engaged and the target sits inside the gap: no current-year
                            saving; may still slow next year's cap growth
      - ``cosmetic``      — cap engaged and the target is above even next year's cap ceiling:
                            no benefit this year or next
    """

    scenario: str
    tax_year: int | None
    market_value: Decimal | None
    capped_value: Decimal | None
    cap_gap: Decimal | None
    cap_engaged: bool
    cap_limit_percent: Decimal
    cap_type: str
    target_market_value: Decimal | None
    required_reduction_for_current_savings: Decimal | None
    current_year_taxable_reduction: Decimal | None
    next_year_ceiling: Decimal | None
    future_year_taxable_reduction: Decimal | None
    estimated_current_savings: Decimal | None
    estimated_future_savings: Decimal | None
    notes: list[str] = field(default_factory=list)


def _money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def is_homestead_capped(entry: AssessmentHistory) -> bool:
    """True when HCAD flags the account as homestead-capped (Cap_acct = Y)."""
    return str(entry.cap_account or "").strip().upper() in {"Y", "YES", "1", "TRUE"}


def _missing(reason: str) -> CapGapResult:
    return CapGapResult(
        scenario="missing",
        tax_year=None,
        market_value=None,
        capped_value=None,
        cap_gap=None,
        cap_engaged=False,
        cap_limit_percent=HOMESTEAD_LIMIT_PERCENT,
        cap_type="homestead",
        target_market_value=None,
        required_reduction_for_current_savings=None,
        current_year_taxable_reduction=None,
        next_year_ceiling=None,
        future_year_taxable_reduction=None,
        estimated_current_savings=None,
        estimated_future_savings=None,
        notes=[reason],
    )


def evaluate_cap_gap(
    entry: AssessmentHistory | None,
    target_market_value: Decimal | None,
    combined_rate: Decimal | None = None,
) -> CapGapResult:
    """Determine whether a market-value protest can lower this year's tax bill.

    Texas caps the taxable ("appraised") value of a qualifying homestead at 10%/year
    growth (Tax Code 23.23) — 20% for non-homestead real property under the temporary
    circuit-breaker limit. When the cap is engaged, market value sits above the capped
    value, and a protest only reduces the current year's bill if it pushes market value
    below the capped value. Otherwise the win, at best, slows future cap growth.

    ``entry`` is the latest AssessmentHistory row. ``target_market_value`` is the
    comparable-implied value the protest argues for (median comp $/sqft x living area).
    ``combined_rate`` is the summed adopted tax rate per dollar across taxing units,
    when known, used to express reductions in dollars per year.
    """

    if entry is None:
        return _missing("No assessment history is available for this property.")

    market_value = entry.market_value
    capped_value = entry.appraised_value
    notes: list[str] = []

    if capped_value is None:
        capped_value = entry.assessed_value
        if capped_value is not None:
            notes.append(
                "Using the assessed value as the taxable basis; re-import assessment "
                "history to load exact capped/market values."
            )

    if market_value is None and capped_value is not None:
        # For uncapped accounts HCAD reports the same figure in every value column.
        market_value = capped_value
        notes.append("Market value was unavailable; assuming it equals the taxable basis.")

    if market_value is None or capped_value is None:
        return _missing("Assessment records for this property are missing value data.")

    if is_homestead_capped(entry):
        cap_type = "homestead"
        cap_limit_percent = HOMESTEAD_LIMIT_PERCENT
    else:
        cap_type = "circuit_breaker"
        cap_limit_percent = CIRCUIT_BREAKER_LIMIT_PERCENT

    cap_gap = max(ZERO, market_value - capped_value)
    cap_engaged = cap_gap > ZERO
    next_year_ceiling = capped_value * (ONE + cap_limit_percent / ONE_HUNDRED)

    target = target_market_value
    required_reduction = cap_gap if cap_engaged else None
    current_reduction: Decimal | None = None
    future_reduction: Decimal | None = None

    if target is None:
        scenario = "no_target"
    elif target >= market_value:
        scenario = "already_below"
    elif not cap_engaged:
        scenario = "direct"
        current_reduction = capped_value - target
    elif target < capped_value:
        scenario = "effective"
        current_reduction = capped_value - target
    elif target < next_year_ceiling:
        scenario = "blocked"
        future_reduction = next_year_ceiling - target
    else:
        scenario = "cosmetic"

    estimated_current_savings = None
    estimated_future_savings = None
    if combined_rate is not None and combined_rate > ZERO:
        if current_reduction is not None:
            estimated_current_savings = _money(current_reduction * combined_rate)
        if future_reduction is not None:
            estimated_future_savings = _money(future_reduction * combined_rate)

    if cap_type == "circuit_breaker":
        notes.append(
            "This account is not flagged as homestead-capped; the temporary 20% "
            "circuit-breaker limit is assumed. Non-homestead property above $5M has "
            "no cap, in which case every dollar of reduction lowers the taxable basis."
        )
    if scenario == "blocked":
        notes.append(
            "Next year's ceiling excludes new improvements, and assumes next year's "
            "market value stays at or above the protested value."
        )

    return CapGapResult(
        scenario=scenario,
        tax_year=entry.tax_year,
        market_value=_money(market_value),
        capped_value=_money(capped_value),
        cap_gap=_money(cap_gap),
        cap_engaged=cap_engaged,
        cap_limit_percent=cap_limit_percent,
        cap_type=cap_type,
        target_market_value=_money(target) if target is not None else None,
        required_reduction_for_current_savings=_money(required_reduction),
        current_year_taxable_reduction=_money(current_reduction),
        next_year_ceiling=_money(next_year_ceiling),
        future_year_taxable_reduction=_money(future_reduction),
        estimated_current_savings=estimated_current_savings,
        estimated_future_savings=estimated_future_savings,
        notes=notes,
    )
