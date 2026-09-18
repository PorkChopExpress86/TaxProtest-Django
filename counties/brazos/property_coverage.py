"""Brazos candidate readiness reuses its authoritative snapshot projection."""

from counties.brazos.models import PropertyAccount, PropertyLand
from counties.brazos.readiness import BrazosActiveSnapshotReadiness
from counties.common.import_coverage import OutcomePopulation


def outcome_populations(*, claimed_gis=False, deliberately_absent_gis=False):
    reader = BrazosActiveSnapshotReadiness()
    snapshot = reader.active_snapshot()
    names = ("search", "comparable", "report", "tax")
    eligible = {name: set() for name in names}
    exclusions = {name: {} for name in names}
    accounts = list(PropertyAccount.objects.filter(tax_year=snapshot.tax_year)) if snapshot else []
    for account in accounts:
        projection = reader.project(account.prop_id)
        if projection is None:
            continue
        for name, ready in (
            ("search", projection.search_ready),
            ("comparable", projection.comparable_ready),
            ("report", projection.report_ready),
            ("tax", projection.tax_impact_ready),
        ):
            if ready and not (deliberately_absent_gis and name != "search"):
                eligible[name].add(account.prop_id)
            else:
                exclusions[name][account.prop_id] = [
                    projection.reason_for(name)
                    or "GIS deliberately absent from explicit Partial candidate"
                ]
    structural_inputs = any(account.living_area is not None for account in accounts) or bool(
        snapshot
        and PropertyLand.objects.filter(tax_year=snapshot.tax_year, acreage__isnull=False).exists()
    )
    has_gis = claimed_gis or any(
        account.coordinate_source and account.coordinate_source_year is not None
        for account in accounts
    )
    comparable_supported = has_gis and structural_inputs and not deliberately_absent_gis
    report_supported = (
        comparable_supported
        and len(accounts) >= 4
        and any(
            account.assessed_value is not None and account.living_area is not None
            for account in accounts
        )
    )
    # A tax gap is independently unavailable. The authoritative projection
    # requires complete matching-year rates and values before claiming it.
    tax_supported = bool(eligible["tax"]) and not deliberately_absent_gis
    supported = {
        "search": snapshot is not None,
        "comparable": comparable_supported,
        "report": report_supported,
        "tax": tax_supported,
    }
    reasons = {
        "search": "Active property snapshot unavailable",
        "comparable": "GIS or structural comparable prerequisites unavailable",
        "report": "Equity facts and at least three qualifying comparables are required",
        "tax": "Complete matching-year report, jurisdiction, values and rates are required",
    }
    return {
        name: OutcomePopulation(
            eligible[name],
            supported=supported[name],
            reason="" if supported[name] else reasons[name],
            exclusions=exclusions[name],
            deliberately_absent=deliberately_absent_gis and name != "search",
        )
        for name in names
    }
