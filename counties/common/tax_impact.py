from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from counties.common.tax_models import AssessmentHistory, PropertyJurisdictionExemption, TaxUnitRate

MONEY_QUANT = Decimal("0.01")
ZERO = Decimal("0")
ONE_HUNDRED = Decimal("100")


class TaxCompleteness(StrEnum):
    """How much of a tax impact could be computed: the closed set every surface reads."""

    MISSING = "missing"
    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass
class TaxImpactResult:
    tax_year: int | None
    current_tax_owed: Decimal
    median_tax_owed: Decimal
    estimated_savings: Decimal
    effective_rate: Decimal
    current_assessed_value: Decimal | None
    taxable_value_used: Decimal | None
    completeness: TaxCompleteness
    warnings: list[str]
    exemptions_summary: list[dict[str, object]]
    per_unit_breakdown: list[dict[str, object]]

    def __post_init__(self) -> None:
        # Accepts the spelling and rejects anything outside the set.
        self.completeness = TaxCompleteness(self.completeness)

    @property
    def may_show_totals(self) -> bool:
        """The one answer to whether the current, median and savings totals may be shown."""
        return self.completeness is TaxCompleteness.COMPLETE

    @property
    def is_partial(self) -> bool:
        return self.completeness is TaxCompleteness.PARTIAL


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


def taxing_units(names: Mapping[str, str]) -> str:
    """Name taxing units for a readiness reason, e.g. ``taxing units B (HC MUD 9), C``."""
    labels = [f"{code} ({name})" if name else code for code, name in sorted(names.items())]
    return f"taxing unit{'s' if len(labels) > 1 else ''} {', '.join(labels)}"


def unavailable_tax_impact(tax_year: int | None, *warnings: str) -> TaxImpactResult:
    """Shared shape for a tax impact that prices nothing.

    Every caller stops before any unit has been priced, so every totals field
    is zero/empty save the warnings explaining why.
    """
    return TaxImpactResult(
        tax_year=tax_year,
        current_tax_owed=ZERO,
        median_tax_owed=ZERO,
        estimated_savings=ZERO,
        effective_rate=ZERO,
        current_assessed_value=None,
        taxable_value_used=None,
        completeness=TaxCompleteness.MISSING,
        warnings=list(warnings),
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
    non_levying: Mapping[str, str] | None = None,
) -> TaxImpactResult:
    """Compute current-vs-median annual tax impact from imported tax data.

    ``county`` scopes every query to one county's rows in the shared
    AssessmentHistory/TaxUnitRate/PropertyJurisdictionExemption tables (see
    wayfinder ticket #9) — required because tax_unit_code alone isn't
    guaranteed unique across counties.

    ``non_levying`` maps the county's units that levy no ad valorem tax to the
    reason shown for them (ADR-0019); they need no rate and add no tax.
    Without a median assessed value the result is partial and claims no savings.
    """
    non_levying = non_levying or {}

    resolved_year = tax_year or _latest_assessment_year(account_number, county)
    warnings: list[str] = []
    breakdown: list[dict[str, object]] = []
    exemptions_summary: list[dict[str, object]] = []

    if resolved_year is None:
        return unavailable_tax_impact(None, "No assessment year is available for this account.")

    median_value = _to_decimal(median_assessed_value)
    if median_value is None or median_value < ZERO:
        median_value = None
        warnings.append("Median assessed value is missing; median tax scenario was not computed.")

    unit_rows = list(
        PropertyJurisdictionExemption.objects.filter(
            account_number=account_number,
            tax_year=resolved_year,
            county=county,
        ).order_by("tax_unit_code", "exemption_code")
    )

    if not unit_rows:
        return unavailable_tax_impact(
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

    if any(
        unit.tax_unit_code not in rate_map and unit.tax_unit_code not in non_levying
        for unit in unit_bases
    ):
        warnings.append("One or more jurisdiction rates are missing for this tax year.")

    known_units = 0
    missing_units = 0
    current_total = ZERO
    median_total = ZERO
    total_rate = ZERO
    total_taxable_used = ZERO

    for unit in unit_bases:
        unit_records = [r for r in unit_rows if r.tax_unit_code == unit.tax_unit_code]
        if any(
            r.exemption_code and r.exemption_amount is None and r.exemption_percent is None
            for r in unit_records
        ):
            missing_units += 1
            warnings.append(f"Exemption inputs are unverified for {unit.tax_unit_code}.")
            continue
        if unit.exemption_code:
            missing_units += 1
            warnings.append(f"Gross jurisdiction base is missing for {unit.tax_unit_code}.")
            continue
        if unit.tax_unit_code in non_levying:
            breakdown.append(
                {
                    "tax_unit_code": unit.tax_unit_code,
                    "tax_unit_name": unit.tax_unit_name,
                    "rate": None,
                    "current_taxable_value": None,
                    "median_taxable_value": None,
                    "current_tax_amount": None,
                    "median_tax_amount": None,
                    "warning": non_levying[unit.tax_unit_code],
                }
            )
            continue
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
            median_value,
            unit_records,
        )

        current_tax = current_taxable * rate
        median_tax = median_taxable * rate if median_taxable is not None else None

        current_total += current_tax
        if median_tax is not None:
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
                "median_tax_amount": _money(median_tax) if median_tax is not None else None,
                "warning": None,
            }
        )
        exemptions_summary.extend(exemptions_applied)

    if known_units == 0:
        completeness = TaxCompleteness.MISSING
    elif missing_units > 0 or median_value is None:
        completeness = TaxCompleteness.PARTIAL
    else:
        completeness = TaxCompleteness.COMPLETE

    if completeness is not TaxCompleteness.COMPLETE:
        warnings.append("Tax impact is partial because one or more required inputs were missing.")

    current_total = _money(current_total)
    median_total = _money(median_total)
    savings = ZERO if median_value is None else _money(max(ZERO, current_total - median_total))
    # The combined rate of every levying unit that was priced.
    effective_rate = total_rate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

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
