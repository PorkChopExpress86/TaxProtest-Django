from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from data.models import AssessmentHistory, PropertyJurisdictionExemption, TaxUnitRate

MONEY_QUANT = Decimal("0.01")
ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")
FALLBACK_YEAR_LOOKBACK = 5

NO_SCOPE_IMPACT = "This does not affect the comparable-property evidence above."


@dataclass
class TaxImpactResult:
    tax_year: int | None
    current_tax_owed: Decimal | None
    median_tax_owed: Decimal | None
    estimated_savings: Decimal | None
    effective_rate: Decimal | None
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


def _latest_assessment_year(account_number: str) -> int | None:
    row = (
        AssessmentHistory.objects.filter(account_number=account_number)
        .order_by("-tax_year")
        .values_list("tax_year", flat=True)
        .first()
    )
    return int(row) if row is not None else None


def _has_usable_tax_data(account_number: str, year: int) -> bool:
    """True if this year has jurisdiction/exemption rows AND at least one matching adopted rate."""
    unit_codes = list(
        PropertyJurisdictionExemption.objects.filter(
            account_number=account_number, tax_year=year
        ).values_list("tax_unit_code", flat=True)
    )
    if not unit_codes:
        return False
    return TaxUnitRate.objects.filter(tax_year=year, tax_unit_code__in=unit_codes).exists()


def _resolve_usable_tax_year(account_number: str, preferred_year: int) -> tuple[int, bool]:
    """Return (year_to_use, used_fallback).

    County appraisal rolls certify for the new tax year before taxing units formally
    adopt rates (typically Sept/Oct), so the newest assessment year can briefly have
    jurisdiction/exemption rows imported but no matching rate data yet. Fall back to the
    most recent earlier year that has both, rather than reporting the whole feature missing.
    """
    if _has_usable_tax_data(account_number, preferred_year):
        return preferred_year, False

    candidate_years = (
        PropertyJurisdictionExemption.objects.filter(
            account_number=account_number,
            tax_year__lt=preferred_year,
            tax_year__gte=preferred_year - FALLBACK_YEAR_LOOKBACK,
        )
        .order_by("-tax_year")
        .values_list("tax_year", flat=True)
        .distinct()
    )
    for year in candidate_years:
        if _has_usable_tax_data(account_number, int(year)):
            return int(year), True

    return preferred_year, False


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


def calculate_tax_impact(
    account_number: str,
    tax_year: int | None,
    median_assessed_value: Decimal | float | int | None,
) -> TaxImpactResult:
    """Compute current-vs-median annual tax impact from imported HCAD tax data."""

    requested_year = tax_year or _latest_assessment_year(account_number)
    resolved_year, used_fallback_year = (
        (None, False)
        if requested_year is None
        else _resolve_usable_tax_year(account_number, requested_year)
    )
    warnings: list[str] = []
    breakdown: list[dict[str, object]] = []
    exemptions_summary: list[dict[str, object]] = []

    if resolved_year is None:
        return TaxImpactResult(
            tax_year=None,
            current_tax_owed=None,
            median_tax_owed=None,
            estimated_savings=None,
            effective_rate=None,
            current_assessed_value=None,
            taxable_value_used=None,
            completeness="missing",
            warnings=[
                f"We don't have an assessment year on file for this account yet. {NO_SCOPE_IMPACT}"
            ],
            exemptions_summary=[],
            per_unit_breakdown=[],
        )

    median_value = _to_decimal(median_assessed_value)
    median_available = median_value is not None and median_value >= ZERO
    if not median_available:
        median_value = None
        warnings.append(
            f"We don't have a comparable median value to estimate savings against yet. {NO_SCOPE_IMPACT}"
        )

    unit_rows = list(
        PropertyJurisdictionExemption.objects.filter(
            account_number=account_number,
            tax_year=resolved_year,
        ).order_by("tax_unit_code", "exemption_code")
    )

    if not unit_rows:
        return TaxImpactResult(
            tax_year=resolved_year,
            current_tax_owed=None,
            median_tax_owed=None,
            estimated_savings=None,
            effective_rate=None,
            current_assessed_value=None,
            taxable_value_used=None,
            completeness="missing",
            warnings=[
                "We don't yet have county tax rate/exemption data for this account and year, "
                f"so we can't estimate your tax bill in dollars right now. {NO_SCOPE_IMPACT}"
            ],
            exemptions_summary=[],
            per_unit_breakdown=[],
        )

    assessment = (
        AssessmentHistory.objects.filter(account_number=account_number, tax_year=resolved_year)
        .values_list("assessed_value", flat=True)
        .first()
    )
    current_assessed_value = _to_decimal(assessment)

    rate_map = {
        row.tax_unit_code: row
        for row in TaxUnitRate.objects.filter(
            tax_year=resolved_year,
            tax_unit_code__in=[row.tax_unit_code for row in unit_rows],
        )
    }

    unit_bases = _dedupe_units(unit_rows)

    if len(rate_map) < len(unit_bases):
        warnings.append(
            f"County tax rates aren't published yet for one or more taxing units this year. {NO_SCOPE_IMPACT}"
        )

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
            warnings.append(
                f"We're missing the taxable value for one or more taxing units. {NO_SCOPE_IMPACT}"
            )
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
        current_taxable = taxable_base
        median_taxable = median_value if median_value is not None else None
        exemptions_applied: list[dict[str, object]] = []

        for rec in unit_records:
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
                "median_tax_amount": _money(median_tax) if median_taxable is not None else None,
                "warning": None,
            }
        )
        exemptions_summary.extend(exemptions_applied)

    if known_units == 0:
        completeness = "missing"
    elif missing_units > 0 or len(rate_map) < len(unit_bases) or not median_available:
        completeness = "partial"
    else:
        completeness = "complete"

    if used_fallback_year:
        warnings.insert(
            0,
            f"County tax rate/exemption data for {requested_year} isn't published yet, so this "
            f"estimate uses the most recent available year ({resolved_year}) instead. {NO_SCOPE_IMPACT}",
        )

    if known_units == 0:
        # No rate matched any unit: neither current nor median totals were actually computed.
        current_total = None
        median_total = None
        savings = None
        effective_rate = None
    else:
        current_total = _money(current_total)
        effective_rate = (total_rate / Decimal(known_units)).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        if median_available:
            median_total = _money(median_total)
            savings = _money(max(ZERO, current_total - median_total))
        else:
            median_total = None
            savings = None

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
