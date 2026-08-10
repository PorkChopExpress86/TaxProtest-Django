"""Cap-status evaluation for the shared assessment-history report.

Evaluates a year's assessed/appraised value increase against the Texas
homestead cap (10%) or circuit-breaker cap (20%). Genuinely county-neutral in
its math, but the *inputs* it reads are not: ``AssessmentHistory.cap_account``
carries a different vocabulary per county (see ``COUNTIES_WITH_TYPED_CAP_FLAG``
below), and this module is the one place that decodes it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from counties.common.tax_models import AssessmentHistory

TEN_PERCENT_CAP = Decimal("10")
TWENTY_PERCENT_CAP = Decimal("20")
ONE_HUNDRED = Decimal("100")
CENT = Decimal("0.01")

#: HCAD's ``Cap_acct`` is a flag, not a presence marker: it is "Y", "N", or
#: "Pending" on every row. Only "Y" means the homestead cap applies. Testing it
#: for non-emptiness would put all 7.9M rows under the 10% homestead cap,
#: including the ~5.5M marked "N" that are subject to the 20% circuit breaker.
HOMESTEAD_CAP_FLAG = "Y"

#: Counties whose ``cap_account`` flag is HCAD's own Y/N/Pending vocabulary,
#: where "Y" specifically means the 10% homestead cap applies to that account.
#: Every other county's flag is a *derived* signal rather than a report of cap
#: type -- e.g. Brazos's importer sets "Y" when a year's appraised_value
#: exceeds its assessed_value (some capping reduction was applied), which
#: doesn't say *which* cap did the capping. Reading a derived flag as if it
#: carried HCAD's homestead-specific meaning would assert a cap type BCAD's
#: export never actually reported -- see evaluate_cap_status below.
COUNTIES_WITH_TYPED_CAP_FLAG = {"harris"}


def _percent_change(current: Decimal | None, prior: Decimal | None) -> Decimal | None:
    if current is None or prior is None or prior <= 0:
        return None
    return ((current - prior) / prior * ONE_HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)


def _money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _has_cap_account(entry: AssessmentHistory) -> bool:
    """Whether the 10% homestead cap applies rather than the 20% circuit breaker.

    Only meaningful for counties in ``COUNTIES_WITH_TYPED_CAP_FLAG`` -- callers
    must check that first. "Pending" (a homestead application not yet granted)
    is treated as not capped: the cap is not in force for the year under
    review, and claiming the tighter limit would overstate the owner's case in
    an ARB filing.
    """
    return str(entry.cap_account or "").strip().upper() == HOMESTEAD_CAP_FLAG


def evaluate_cap_status(
    current: AssessmentHistory,
    prior: AssessmentHistory | None = None,
) -> dict[str, Any]:
    """Evaluate assessed/appraised value increase against Texas cap thresholds.

    Cap *type* selection (homestead 10% vs. circuit-breaker 20%) is only
    possible for counties in ``COUNTIES_WITH_TYPED_CAP_FLAG``, whose
    ``cap_account`` flag actually distinguishes them. For any other county,
    the flag only reports that a cap reduction occurred, not which cap
    applied -- so this returns an honest "unknown" cap type with no asserted
    limit_percent/allowed_value/overage, rather than guessing. The
    year-over-year increase_percent is still real data and always returned.
    """
    # Three-tier fallback for last year's value, each tier covering a real gap
    # in the source data rather than a hypothetical one:
    #   1. current.prior_appraised_value -- the county's own export sometimes
    #      carries the prior year's appraised value directly on the current
    #      row (HCAD's snapshot does this); use it first since it's the
    #      county's own stated figure, not something we recomputed.
    #   2. prior.appraised_value -- falls back to actually joining the prior
    #      year's AssessmentHistory row when the current row's own snapshot
    #      didn't carry a prior-year figure (no such field that year, or the
    #      county's export left it blank).
    #   3. prior.assessed_value -- last resort when even the prior year's row
    #      is missing an appraised_value (e.g. an incomplete prior-year
    #      import). assessed_value is an acceptable stand-in because Texas
    #      assessed value is capped at appraised value (assessed = min(
    #      appraised, capped value)) -- it's the closest real figure on hand,
    #      not an arbitrary guess.
    prior_value = current.prior_appraised_value or (prior.appraised_value if prior else None)
    if prior_value is None and prior:
        prior_value = prior.assessed_value

    current_value = current.appraised_value or current.assessed_value
    market_value = current.market_value
    new_construction = current.new_construction_value or Decimal("0")
    increase_percent = _percent_change(current_value, prior_value)

    if current.county not in COUNTIES_WITH_TYPED_CAP_FLAG:
        return {
            "status": "unknown",
            "label": "Needs review",
            "cap_type": "unknown",
            "limit_percent": None,
            "increase_percent": increase_percent,
            "allowed_value": None,
            "overage": None,
        }

    cap_type = "homestead" if _has_cap_account(current) else "circuit_breaker"
    limit_percent = TEN_PERCENT_CAP if cap_type == "homestead" else TWENTY_PERCENT_CAP

    if current_value is None or prior_value is None:
        return {
            "status": "unknown",
            "label": "Needs review",
            "cap_type": cap_type,
            "limit_percent": limit_percent,
            "increase_percent": increase_percent,
            "allowed_value": None,
            "overage": None,
        }

    allowed_by_cap = prior_value * (Decimal("1") + (limit_percent / ONE_HUNDRED))
    allowed_by_cap += new_construction
    allowed_value = (
        min(allowed_by_cap, market_value) if market_value is not None else allowed_by_cap
    )
    allowed_value = _money(allowed_value)
    overage = _money(current_value - allowed_value) if allowed_value is not None else None
    status = "over_limit" if overage is not None and overage > 0 else "within_limit"

    return {
        "status": status,
        "label": "Over cap" if status == "over_limit" else "Within cap",
        "cap_type": cap_type,
        "limit_percent": limit_percent,
        "increase_percent": increase_percent,
        "allowed_value": allowed_value,
        "overage": overage,
    }
