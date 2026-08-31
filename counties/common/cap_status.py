"""Cap-status evaluation for the shared assessment-history report.

Evaluates a year's assessed/appraised value increase against the Texas
homestead cap (Tax Code 23.23, 10%) or circuit-breaker cap (Tax Code 23.231,
20%). Genuinely county-neutral in its math, but the *inputs* it reads are not:
``AssessmentHistory.cap_account`` carries a different vocabulary per county
(see ``COUNTIES_WITH_TYPED_CAP_FLAG`` below), and this module is the one place
that decodes it.

A cap is a rule with a life, not a constant. 23.23 has capped residence
homesteads since long before the 2022 start of our history, but 23.231 was
added by Acts 2023, 88th Leg., 2nd C.S., Ch. 1 (S.B. 2) effective January 1,
2024 and expires December 31, 2026 -- so tax years 2022 and 2023 had no
non-homestead cap at all, and 2027 onward will have none again unless the
legislature revives it. Selecting a limit from the cap flag alone measures a
2022 row against a rule that postdates it. Every statutory year and value
boundary lives in ``circuit_breaker_ceiling`` below.
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

#: Tax Code 23.231(j)'s qualifying appraised-value ceiling, by tax year: the
#: statute fixes $5,000,000 for 2024 and directs the comptroller to adjust it
#: each following year by the CPI change, rounded to the nearest $10,000.
#: These are the comptroller's published figures.
#:
#: Membership in this mapping is also the section's *life*: 23.231 did not
#: exist before tax year 2024 and 23.231(k) expires it December 31, 2026, so a
#: year with no published ceiling is a year with no circuit breaker. Extending
#: the section would mean adding its new year's ceiling here anyway, which
#: keeps the two facts from drifting apart.
CIRCUIT_BREAKER_VALUE_CEILING: dict[int, Decimal] = {
    2024: Decimal("5000000"),
    2025: Decimal("5160000"),
    2026: Decimal("5320000"),
}


def circuit_breaker_ceiling(tax_year: int) -> Decimal | None:
    """The 23.231 qualifying value ceiling for ``tax_year``, or None.

    None means the circuit breaker was not in force that year at any value --
    before 2024 the section did not exist, and after 2026 it has expired.
    """
    return CIRCUIT_BREAKER_VALUE_CEILING.get(tax_year)


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


def _applicable_cap(
    entry: AssessmentHistory, tax_year_value: Decimal | None
) -> tuple[str, Decimal | None]:
    """Which Texas cap governed this row's tax year, and at what percent.

    Returns ``("homestead", 10)`` or ``("circuit_breaker", 20)`` when a cap was
    in force and this row can establish the property is subject to it,
    ``("none", None)`` when no cap existed for that tax year, and
    ``("unknown", None)`` when a cap may be in force but this row cannot
    establish that the property qualifies.

    ``tax_year_value`` is tested against the 23.231(b) ceiling. The statute
    measures the ceiling against the appraised value "for the tax year in which
    the property first qualifies", and qualification then continues while the
    same owner holds the property. Assessment history does not include the
    ownership date needed to recover that first year. Testing the current row's
    appraised value is therefore conservative: it can leave a long-qualified
    property above the ceiling at "needs review", but it will not manufacture a
    20% limit from a preceding-year value below the ceiling.

    Two further 23.231 exclusions stay unmodelled because no field here
    reports them: the limitation only takes effect the tax year *after* the
    first year the owner owns the property on January 1 (23.231(f)), and it
    never applies to property under a special appraisal such as agricultural
    or timberland.
    """
    if _has_cap_account(entry):
        # 23.23's homestead cap predates our earliest history year and has no
        # value ceiling, so neither gate below applies to it.
        return "homestead", TEN_PERCENT_CAP

    ceiling = circuit_breaker_ceiling(entry.tax_year)
    if ceiling is None:
        return "none", None
    if tax_year_value is None or tax_year_value > ceiling:
        return "unknown", None
    return "circuit_breaker", TWENTY_PERCENT_CAP


def _no_limit_result(cap_type: str, increase_percent: Decimal | None) -> dict[str, Any]:
    """A row no limit is attached to, distinguishing the two reasons why.

    ``"none"`` is a finding: no Texas cap governed that tax year, so there is
    nothing for the value to be over. ``"unknown"`` is an absence of evidence:
    a cap may well apply, but this row cannot establish it. Both keep the
    county-neutral ``increase_percent``, which is real data either way.
    """
    status, label = (
        ("not_applicable", "No cap in force") if cap_type == "none" else ("unknown", "Needs review")
    )
    return {
        "status": status,
        "label": label,
        "cap_type": cap_type,
        "limit_percent": None,
        "increase_percent": increase_percent,
        "allowed_value": None,
        "overage": None,
    }


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

    Selection is then gated on the row's tax year and value by
    ``_applicable_cap``, because the circuit breaker is a 2024-2026 provision
    with a qualifying value ceiling rather than a standing rule.
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
        return _no_limit_result("unknown", increase_percent)

    # The 23.231 ceiling is expressly an appraised-value test. Keep the
    # assessed-value fallback for the year-over-year trend and homestead math,
    # but never use it to manufacture circuit-breaker eligibility when the
    # appraised value is absent.
    cap_type, limit_percent = _applicable_cap(current, current.appraised_value)
    if limit_percent is None:
        return _no_limit_result(cap_type, increase_percent)

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
