from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from counties.common.tax_models import AssessmentHistory, PropertyJurisdictionExemption, TaxUnitRate

MONEY_QUANT = Decimal("0.01")
ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


@dataclass
class TaxImpactResult:
    tax_year: int | None
    current_tax_owed: Decimal
    median_tax_owed: Decimal
    estimated_savings: Decimal
    effective_rate: Decimal
    current_assessed_value: Decimal | None
    taxable_value_used: Decimal | None
    completeness: str
    warnings: list[str]
    exemptions_summary: list[dict[str, object]]
    per_unit_breakdown: list[dict[str, object]]


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _latest_assessment_year(account_number: str, county: str) -> int | None:
    """Newest tax year this account can actually be costed for.

    Jurisdiction rows come first because they are what the calculation consumes:
    without a taxing unit and a rate there is nothing to compute, however much
    assessment history exists. The two are loaded from different HCAD archives
    and routinely sit a year apart -- assessment history reaches 2026 while the
    jurisdiction extract is still 2025 -- so preferring the history year would
    resolve to a year with no units and report "missing" on data we hold.

    Assessment history is the fallback, and separately supplies the assessed
    value displayed beside the estimate when a row exists for the chosen year.
    """
    for queryset in (
        PropertyJurisdictionExemption.objects.filter(account_number=account_number, county=county),
        AssessmentHistory.objects.filter(account_number=account_number, county=county),
    ):
        row = queryset.order_by("-tax_year").values_list("tax_year", flat=True).first()
        if row is not None:
            return int(row)
    return None


def _dedupe_units(rows: list[PropertyJurisdictionExemption]) -> list[PropertyJurisdictionExemption]:
    seen: set[str] = set()
    out: list[PropertyJurisdictionExemption] = []
    for row in rows:
        code = (row.tax_unit_code or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(row)
    return out


def _empty_result(tax_year: int | None, warning: str) -> TaxImpactResult:
    """Shared shape for the two "nothing to compute" early returns.

    Both callers stop before any unit has been priced, so every totals field
    is zero/empty save the single warning explaining why.
    """
    return TaxImpactResult(
        tax_year=tax_year,
        current_tax_owed=ZERO,
        median_tax_owed=ZERO,
        estimated_savings=ZERO,
        effective_rate=ZERO,
        current_assessed_value=None,
        taxable_value_used=None,
        completeness="missing",
        warnings=[warning],
        exemptions_summary=[],
        per_unit_breakdown=[],
    )


def _apply_exemptions(
    current_base: Decimal,
    median_base: Decimal | None,
    records: list[PropertyJurisdictionExemption],
) -> tuple[Decimal, Decimal | None, list[dict[str, object]]]:
    """Apply one taxing unit's exemption records to a starting taxable value.

    For each record, in order, apply its fixed exemption amount and then its
    percent exemption, clamping the running taxable value at zero after each
    step, to both the current and median bases. Fixed-before-percent is a
    deliberate sequencing choice; the zero-floor is a deliberate business
    rule -- this is Texas ARB math, not incidental plumbing. Returns the
    final current/median taxable values plus a before/after audit-trail entry
    per record that actually changed something.
    """
    current_taxable = current_base
    median_taxable = median_base
    exemptions_applied: list[dict[str, object]] = []

    for rec in records:
        fixed = _to_decimal(rec.exemption_amount)
        percent = _to_decimal(rec.exemption_percent)
        if fixed is None and percent is None:
            continue

        before_current = current_taxable
        before_median = median_taxable

        if fixed is not None:
            current_taxable = max(ZERO, current_taxable - fixed)
            if median_taxable is not None:
                median_taxable = max(ZERO, median_taxable - fixed)
        if percent is not None and percent > ZERO:
            pct = percent / ONE_HUNDRED
            current_taxable = max(ZERO, current_taxable * (Decimal("1") - pct))
            if median_taxable is not None:
                median_taxable = max(ZERO, median_taxable * (Decimal("1") - pct))

        exemptions_applied.append(
            {
                "tax_unit_code": rec.tax_unit_code,
                "exemption_code": rec.exemption_code,
                "description": rec.exemption_description,
                "fixed_amount": fixed,
                "percent": percent,
                "before_current": _money(before_current),
                "after_current": _money(current_taxable),
                "before_median": _money(before_median) if before_median is not None else None,
                "after_median": _money(median_taxable) if median_taxable is not None else None,
            }
        )

    return current_taxable, median_taxable, exemptions_applied


def calculate_tax_impact(
    account_number: str,
    tax_year: int | None,
    median_assessed_value: Decimal | float | int | None,
    county: str = "harris",
) -> TaxImpactResult:
    """Compute current-vs-median annual tax impact from imported tax data.

    ``county`` scopes every query to one county's rows in the shared
    AssessmentHistory/TaxUnitRate/PropertyJurisdictionExemption tables (see
    wayfinder ticket #9) — required because tax_unit_code alone isn't
    guaranteed unique across counties.
    """

    resolved_year = tax_year or _latest_assessment_year(account_number, county)
    warnings: list[str] = []
    breakdown: list[dict[str, object]] = []
    exemptions_summary: list[dict[str, object]] = []

    if resolved_year is None:
        return _empty_result(None, "No assessment year is available for this account.")

    median_value = _to_decimal(median_assessed_value)
    if median_value is None or median_value < ZERO:
        warnings.append("Median assessed value is missing; median tax scenario was not computed.")

    unit_rows = list(
        PropertyJurisdictionExemption.objects.filter(
            account_number=account_number,
            tax_year=resolved_year,
            county=county,
        ).order_by("tax_unit_code", "exemption_code")
    )

    if not unit_rows:
        return _empty_result(
            resolved_year, "No jurisdiction/exemption rows were found for this account and year."
        )

    assessment = (
        AssessmentHistory.objects.filter(
            account_number=account_number, tax_year=resolved_year, county=county
        )
        .values_list("assessed_value", flat=True)
        .first()
    )
    current_assessed_value = _to_decimal(assessment)

    rate_map = {
        row.tax_unit_code: row
        for row in TaxUnitRate.objects.filter(
            tax_year=resolved_year,
            tax_unit_code__in=[row.tax_unit_code for row in unit_rows],
            county=county,
        )
    }

    unit_bases = _dedupe_units(unit_rows)

    if len(rate_map) < len(unit_bases):
        warnings.append("One or more jurisdiction rates are missing for this tax year.")

    known_units = 0
    missing_units = 0
    current_total = ZERO
    median_total = ZERO
    total_rate = ZERO
    total_taxable_used = ZERO

    for unit in unit_bases:
        rate_row = rate_map.get(unit.tax_unit_code)
        rate = _to_decimal(rate_row.adopted_rate if rate_row else None)
        if rate is None:
            missing_units += 1
            breakdown.append(
                {
                    "tax_unit_code": unit.tax_unit_code,
                    "tax_unit_name": unit.tax_unit_name,
                    "rate": None,
                    "current_taxable_value": None,
                    "median_taxable_value": None,
                    "current_tax_amount": None,
                    "median_tax_amount": None,
                    "warning": "Missing tax-unit rate",
                }
            )
            continue

        known_units += 1
        total_rate += rate

        unit_records = [r for r in unit_rows if r.tax_unit_code == unit.tax_unit_code]

        taxable_base = _to_decimal(unit.taxable_value)
        if taxable_base is None:
            taxable_base = current_assessed_value

        if taxable_base is None:
            missing_units += 1
            warnings.append("One or more units are missing current taxable/assessed value.")
            breakdown.append(
                {
                    "tax_unit_code": unit.tax_unit_code,
                    "tax_unit_name": unit.tax_unit_name,
                    "rate": rate,
                    "current_taxable_value": None,
                    "median_taxable_value": None,
                    "current_tax_amount": None,
                    "median_tax_amount": None,
                    "warning": "Missing taxable value",
                }
            )
            continue

        # Apply fixed + percent exemptions if present for each unit in deterministic order.
        current_taxable, median_taxable, exemptions_applied = _apply_exemptions(
            taxable_base,
            median_value if median_value is not None else None,
            unit_records,
        )

        current_tax = current_taxable * rate
        median_tax = median_taxable * rate if median_taxable is not None else ZERO

        current_total += current_tax
        median_total += median_tax
        total_taxable_used += current_taxable

        breakdown.append(
            {
                "tax_unit_code": unit.tax_unit_code,
                "tax_unit_name": unit.tax_unit_name,
                "rate": rate,
                "current_taxable_value": _money(current_taxable),
                "median_taxable_value": (
                    _money(median_taxable) if median_taxable is not None else None
                ),
                "current_tax_amount": _money(current_tax),
                "median_tax_amount": _money(median_tax),
                "warning": None,
            }
        )
        exemptions_summary.extend(exemptions_applied)

    if known_units == 0:
        completeness = "missing"
    elif missing_units > 0 or len(rate_map) < len(unit_bases):
        completeness = "partial"
    else:
        completeness = "complete"

    if completeness != "complete":
        warnings.append("Tax impact is partial because one or more required inputs were missing.")

    current_total = _money(current_total)
    median_total = _money(median_total)
    savings = _money(max(ZERO, current_total - median_total))
    effective_rate = (total_rate / Decimal(max(known_units, 1))).quantize(
        Decimal("0.000001"), rounding=ROUND_HALF_UP
    )

    return TaxImpactResult(
        tax_year=resolved_year,
        current_tax_owed=current_total,
        median_tax_owed=median_total,
        estimated_savings=savings,
        effective_rate=effective_rate,
        current_assessed_value=current_assessed_value,
        taxable_value_used=_money(total_taxable_used) if total_taxable_used else None,
        completeness=completeness,
        warnings=warnings,
        exemptions_summary=exemptions_summary,
        per_unit_breakdown=breakdown,
    )
