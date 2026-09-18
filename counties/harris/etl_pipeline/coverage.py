"""Harris readiness populations using its canonical county read seams."""

from counties.common.analysis import MIN_COMPS_FOR_RECOMMENDATION
from counties.common.import_coverage import OutcomePopulation
from counties.common.tax_models import PropertyJurisdictionExemption, TaxUnitRate
from counties.harris.adapter import adapter
from counties.harris.models import BuildingDetail, PropertyRecord


def outcome_populations(year: int | None, *, claimed_gis=False) -> dict[str, OutcomePopulation]:
    buildings = {}
    for building in BuildingDetail.objects.filter(is_active=True).order_by("id").iterator():
        buildings.setdefault(building.account_number, building)
    ready, located, equity_inputs, exclusions = set(), set(), set(), {}
    any_coordinates = False
    for prop in PropertyRecord.objects.all().iterator():
        key = prop.account_number
        any_coordinates |= prop.latitude is not None and prop.longitude is not None
        reasons = []
        if not prop.is_residential:
            reasons.append("Not residential under Harris source classification")
        if not prop.is_data_ready:
            building = buildings.get(key)
            if prop.latitude is None or prop.longitude is None:
                reasons.append("Coordinates unavailable")
            if building is None or building.bedrooms is None or building.bathrooms is None:
                reasons.append("Active bedroom/bathroom facts unavailable")
            if not reasons:
                reasons.append("Harris data readiness incomplete")
        if reasons:
            exclusions[key] = reasons
        else:
            ready.add(key)
            if prop.latitude is not None and prop.longitude is not None:
                located.add(key)
        building = buildings.get(key)
        area = building.heat_area if building and building.heat_area else prop.building_area
        value = prop.assessed_value or prop.value
        if area and area > 0 and value and value > 0:
            equity_inputs.add(key)
    report_supported = len(equity_inputs) >= MIN_COMPS_FOR_RECOMMENDATION + 1
    report_ready = set()
    report_exclusions = dict(exclusions)
    for key in ready:
        if key not in equity_inputs or key not in located:
            report_exclusions[key] = [
                "Positive assessed value, living area and coordinates are required"
            ]
        elif report_supported:
            comps = adapter.find_comps(key, max_distance_miles=10.0, max_results=50, min_score=30.0)
            if (
                sum(comp.value_per_sqft is not None for comp in comps)
                >= MIN_COMPS_FOR_RECOMMENDATION
            ):
                report_ready.add(key)
            else:
                report_exclusions[key] = ["At least three qualifying comparables are required"]
    tax_prerequisites = bool(
        year
        and report_supported
        and TaxUnitRate.objects.filter(county="harris", tax_year=year).exists()
        and PropertyJurisdictionExemption.objects.filter(county="harris", tax_year=year).exists()
    )
    tax_ready, tax_exclusions = set(), dict(report_exclusions)
    if tax_prerequisites:
        for key in report_ready:
            result = adapter.tax_impact(key, year, None)
            if result.completeness == "complete":
                tax_ready.add(key)
            else:
                tax_exclusions[key] = list(result.warnings)
    tax_supported = bool(tax_ready)
    return {
        "search": OutcomePopulation(ready, exclusions=exclusions),
        "comparable": OutcomePopulation(
            located,
            supported=claimed_gis or any_coordinates,
            reason="" if claimed_gis or any_coordinates else "GIS coordinates unavailable",
            exclusions=exclusions,
        ),
        "report": OutcomePopulation(
            report_ready,
            supported=report_supported,
            reason=(
                ""
                if report_supported
                else "A qualifying equity comparison population is unavailable"
            ),
            exclusions=report_exclusions,
        ),
        "tax": OutcomePopulation(
            tax_ready,
            supported=tax_supported,
            reason=(
                ""
                if tax_supported
                else "Matching-year report, jurisdiction and rate prerequisites unavailable"
            ),
            exclusions=tax_exclusions,
        ),
    }
