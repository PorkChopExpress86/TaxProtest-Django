"""Brazos County (BCAD) views: search (brazos_index) and ARB protest
evidence report (protest_analysis).

Neither is a generalization of taxprotest.views' equivalents (wayfinder
ticket #9): PropertyAccount is a structurally unrelated model to Harris's
PropertyRecord, with no FK relationship to PropertyLand/PropertyImprovement
(all use a plain prop_id CharField — the same pattern Harris uses for
PropertyRecord/BuildingDetail joined by account number). protest_analysis
mirrors taxprotest.views.protest_analysis's report STRUCTURE (subject card,
equity summary, tax impact, $/sqft distribution, comps table) per that
ticket's decision, built fresh against Brazos's models and
brazos_cad.similarity. The pure-data chart/summary helpers below
(_score_breakdown_summary, _ppsf_distribution_chart) have zero PropertyRecord
coupling in Harris's version either, so they're duplicated verbatim rather
than imported, keeping this module self-contained like similarity.py.

Deliberately out of scope this pass: CSV export, PDF export, and a
multi-year assessment history chart (Brazos has no populated
AssessmentHistory rows yet — county="brazos" query is wired for when that
changes, but returns empty today, and the template hides that section
gracefully when it does).
"""

from __future__ import annotations

import statistics
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Max, Sum
from django.http import Http404
from django.shortcuts import render

from data.assessment_history import evaluate_cap_status
from data.models import AssessmentHistory
from data.tax_impact import calculate_tax_impact

from .models import (
    PropertyAccount,
    PropertyExtraFeature,
    PropertyLand,
)
from .similarity import _primary_improvement, find_similar_properties, get_similarity_label

SORT_MAP = {
    "prop_id": "prop_id",
    "owner_name": "owner_name",
    "situs_city": "situs_city",
    "situs_zip": "situs_zip",
}


def _land_totals_by_prop(prop_ids: list[str], tax_year: int) -> dict[str, dict[str, object]]:
    rows = (
        PropertyLand.objects.filter(prop_id__in=prop_ids, tax_year=tax_year)
        .values("prop_id")
        .annotate(total_land_value=Sum("land_value"), total_acreage=Sum("acreage"))
    )
    return {row["prop_id"]: row for row in rows}


def brazos_index(request):
    results = []
    page_obj = None

    query_source = request.GET if request.method == "GET" else request.POST

    owner_name = query_source.get("owner_name", "").strip()
    address = query_source.get("address", "").strip()
    zip_code = query_source.get("zip_code", "").strip()
    page_number = query_source.get("page", "1")
    sort = query_source.get("sort", "owner_name")
    direction = query_source.get("dir", "asc")

    filters_applied = any([owner_name, address, zip_code])

    params = {
        "owner_name": owner_name,
        "address": address,
        "zip_code": zip_code,
        "sort": sort,
        "dir": direction,
    }

    # Only one tax year is ever loaded at a time (load_brazos_cad replaces
    # the prior year's rows), so pick it dynamically rather than hardcoding.
    active_year = PropertyAccount.objects.aggregate(Max("tax_year"))["tax_year__max"]

    if filters_applied and active_year:
        qs = PropertyAccount.objects.filter(tax_year=active_year)
        if owner_name:
            qs = qs.filter(owner_name__icontains=owner_name)
        if address:
            qs = qs.filter(situs_address__icontains=address)
        if zip_code:
            qs = qs.filter(situs_zip__icontains=zip_code)

        primary = SORT_MAP.get(sort, "owner_name")
        prefix = "-" if direction == "desc" else ""
        qs = qs.order_by(f"{prefix}{primary}", "prop_id")

        paginator = Paginator(qs, 200)
        page_obj = paginator.get_page(page_number)
        accounts = list(page_obj.object_list)

        # No FK from PropertyAccount to PropertyLand (both use a plain
        # prop_id CharField) — merge land totals manually, same pattern as
        # Harris's _active_related_maps. Land value/acreage are therefore
        # NOT sortable at the DB level in this pass (see SORT_MAP above).
        land_by_prop = _land_totals_by_prop([a.prop_id for a in accounts], active_year)

        formatted = []
        for account in accounts:
            land = land_by_prop.get(account.prop_id, {})
            formatted.append(
                {
                    "prop_id": account.prop_id,
                    "owner_name": account.owner_name,
                    "address": account.situs_address,
                    "city": account.situs_city,
                    "zip_code": account.situs_zip,
                    "land_value": land.get("total_land_value"),
                    "acreage": land.get("total_acreage"),
                    "has_location": bool(account.latitude and account.longitude),
                }
            )
        results = formatted

    query_params = request.GET.copy()
    page_query = query_params.copy()
    page_query.pop("page", None)
    base_query = page_query.urlencode()

    sort_query_params = page_query.copy()
    sort_query_params.pop("sort", None)
    sort_query_params.pop("dir", None)
    sort_query = sort_query_params.urlencode()

    context = {
        "results": results,
        "page_obj": page_obj,
        "base_query": base_query,
        "sort_query": sort_query,
        "form_values": params,
        "filters_applied": filters_applied,
        "sort": sort,
        "dir": direction,
        "active_year": active_year,
    }

    return render(request, "brazos_index.html", context)


# ---------------------------------------------------------------- protest_analysis helpers


def _score_breakdown_summary(components: list[dict[str, object]]) -> str:
    parts = []
    for component in components:
        if component.get("points") is None:
            continue
        parts.append(f"{component['label']}: {component['points']}/{component['weight']}")
    return "; ".join(parts)


def _ppsf_distribution_chart(
    comp_values: list[float], subject_value: float | None, bins: int = 10
) -> dict[str, object] | None:
    """Pure-data SVG bar-chart layout -- no PropertyRecord coupling in
    Harris's version either (taxprotest/views.py), so duplicated verbatim
    rather than imported, matching similarity.py's approach to shared math."""
    if not comp_values:
        return None

    values = sorted(comp_values)
    min_value = values[0]
    max_value = values[-1]
    if max_value == min_value:
        max_value = min_value + 1.0

    bin_count = max(4, min(12, bins))
    bin_size = (max_value - min_value) / bin_count
    counts = [0] * bin_count
    for value in values:
        index = int((value - min_value) / bin_size)
        if index >= bin_count:
            index = bin_count - 1
        counts[index] += 1

    max_count = max(counts) if counts else 1
    bar_width = 32
    bar_gap = 6
    chart_height = 170
    chart_top = 20
    chart_bottom = 34
    axis_y = chart_top + chart_height

    bars: list[dict[str, object]] = []
    for idx, count in enumerate(counts):
        x = idx * (bar_width + bar_gap)
        height = (count / max_count) * chart_height if max_count else 0
        y = axis_y - height
        low = min_value + (idx * bin_size)
        high = low + bin_size
        bars.append(
            {
                "x": round(x, 2),
                "y": round(y, 2),
                "width": bar_width,
                "height": round(height, 2),
                "count": count,
                "low": round(low, 2),
                "high": round(high, 2),
            }
        )

    width = (bin_count * bar_width) + ((bin_count - 1) * bar_gap)

    average_value = statistics.mean(values)
    average_ratio = max(0.0, min(1.0, (average_value - min_value) / (max_value - min_value)))
    average_x = round(average_ratio * width, 2)

    subject_x = None
    if subject_value is not None:
        ratio = max(0.0, min(1.0, (subject_value - min_value) / (max_value - min_value)))
        subject_x = round(ratio * width, 2)

    tick_indices = sorted({0, max(0, bin_count // 2), bin_count - 1})
    x_ticks: list[dict[str, object]] = []
    for idx in tick_indices:
        bar = bars[idx]
        x_ticks.append(
            {
                "x": round(float(bar["x"]) + (bar_width / 2), 2),
                "label": f"${bar['low']:.0f}-${bar['high']:.0f}",
            }
        )

    return {
        "bars": bars,
        "width": width,
        "height": chart_top + chart_height + chart_bottom,
        "axis_y": axis_y,
        "min_value": round(min_value, 2),
        "max_value": round(max_value, 2),
        "average_value": round(average_value, 2),
        "average_x": average_x,
        "max_count": max_count,
        "subject_x": subject_x,
        "x_ticks": x_ticks,
    }


def _format_feature_list(features: list[PropertyExtraFeature], max_features: int = 10) -> str:
    """Brazos analog of Harris's format_feature_list -- feature_type/
    feature_value instead of feature_code/feature_description (no separate
    description field exists here, see PropertyExtraFeature's docstring)."""
    feature_counts: dict[str, int] = {}
    for feature in features:
        label = feature.feature_type or "Unknown"
        if feature.feature_value:
            label = f"{label} ({feature.feature_value})"
        feature_counts[label] = feature_counts.get(label, 0) + 1

    items = []
    for label, count in sorted(feature_counts.items())[:max_features]:
        items.append(f"{label} x{count}" if count > 1 else label)
    return ", ".join(items) if items else "None"


def _assessment_history_rows(prop_id: str, limit: int = 5) -> list[dict[str, object]]:
    """Scoped county="brazos" -- see module docstring: no Brazos rows exist
    in the shared AssessmentHistory table yet, so this returns [] today.
    Wired now so the report picks up real data automatically once a future
    ingest populates it, without a code change."""
    history = list(
        AssessmentHistory.objects.filter(account_number=prop_id, county="brazos").order_by(
            "-tax_year"
        )[:limit]
    )
    rows = []
    for index, entry in enumerate(history):
        prior = history[index + 1] if index + 1 < len(history) else None
        increase_percent = None
        if entry.assessed_value is not None and prior and prior.assessed_value:
            increase_percent = (
                (entry.assessed_value - prior.assessed_value) / prior.assessed_value * Decimal(100)
            ).quantize(Decimal("0.01"))
        rows.append(
            {
                "tax_year": entry.tax_year,
                "assessed_value": entry.assessed_value,
                "appraised_value": entry.appraised_value,
                "market_value": entry.market_value,
                "increase_percent": increase_percent,
                "cap_status": evaluate_cap_status(entry, prior),
            }
        )
    return rows


def protest_analysis(request, prop_id):
    """ARB protest evidence report: equity comparison + tax impact, same
    report structure as Harris's (wayfinder ticket #9)."""
    target = PropertyAccount.objects.filter(prop_id=prop_id).order_by("-tax_year").first()
    if not target:
        raise Http404("Property not found")

    tax_year = target.tax_year

    if not target.latitude or not target.longitude:
        return render(
            request,
            "brazos_protest_analysis.html",
            {
                "error": "This property does not have location data required for similarity search.",
                "target": target,
            },
        )

    try:
        min_score = float(request.GET.get("min_score", "70.0"))
    except (ValueError, TypeError):
        min_score = 70.0
    min_score = max(52.0, min(100.0, min_score))

    subject_living_area = float(target.living_area) if target.living_area else None
    subject_assessed = target.assessed_value
    subject_value_per_sqft = None
    if subject_assessed and subject_living_area and subject_living_area > 0:
        subject_value_per_sqft = float(subject_assessed) / subject_living_area

    similar = find_similar_properties(
        prop_id, max_distance_miles=10.0, max_results=50, min_score=min_score
    )

    target_improvement, target_building = _primary_improvement(prop_id, tax_year)
    target_year_built = (
        target_improvement.year_built
        if target_improvement and target_improvement.year_built
        else target.year_built
    )

    comps = []
    for result in similar:
        prop = result["property"]
        building = result["building"]
        features = result["features"]

        comp_assessed = prop.assessed_value
        comp_living_area = float(prop.living_area) if prop.living_area else None

        comp_value_per_sqft = None
        comp_delta = None
        if comp_assessed and comp_living_area and comp_living_area > 0:
            comp_value_per_sqft = float(comp_assessed) / comp_living_area
            if subject_value_per_sqft is not None:
                comp_delta = comp_value_per_sqft - subject_value_per_sqft

        comps.append(
            {
                "prop_id": prop.prop_id,
                "address": prop.situs_address,
                "zip_code": prop.situs_zip,
                "assessed_value": comp_assessed,
                "living_area": comp_living_area,
                "comp_value_per_sqft": comp_value_per_sqft,
                "comp_delta": comp_delta,
                "distance": result["distance"],
                "similarity_score": result["similarity_score"],
                "match_label": get_similarity_label(result["similarity_score"]),
                "bedrooms": building.bedrooms if building else None,
                "bathrooms": building.bathrooms if building else None,
                "class_code": prop.class_code,
                "features": _format_feature_list(features, max_features=5),
                "score_breakdown": result.get("score_breakdown", []),
                "score_breakdown_summary": _score_breakdown_summary(
                    result.get("score_breakdown", [])
                ),
            }
        )

    median_comp_value_per_sqft = None
    equity_gap_per_sqft = None
    estimated_savings = None
    comps_below_subject = 0

    qualifying_ppsf = [
        c["comp_value_per_sqft"] for c in comps if c["comp_value_per_sqft"] is not None
    ]
    if subject_value_per_sqft is not None and qualifying_ppsf:
        median_comp_value_per_sqft = statistics.median(qualifying_ppsf)
        equity_gap_per_sqft = subject_value_per_sqft - median_comp_value_per_sqft
        if subject_living_area:
            estimated_savings = max(0.0, equity_gap_per_sqft * subject_living_area)
        comps_below_subject = sum(1 for p in qualifying_ppsf if p < subject_value_per_sqft)

    assessment_history = _assessment_history_rows(prop_id)
    median_assessed_value = None
    if median_comp_value_per_sqft is not None and subject_living_area:
        median_assessed_value = Decimal(str(median_comp_value_per_sqft)) * Decimal(
            str(subject_living_area)
        )
    tax_impact = calculate_tax_impact(
        account_number=prop_id,
        tax_year=assessment_history[0]["tax_year"] if assessment_history else tax_year,
        median_assessed_value=median_assessed_value,
        county="brazos",
    )

    context = {
        "target": target,
        "target_building": target_building,
        "target_year_built": target_year_built,
        "target_features": _format_feature_list(
            list(PropertyExtraFeature.objects.filter(prop_id=prop_id, tax_year=tax_year))
        ),
        "assessment_history": assessment_history,
        "subject_living_area": subject_living_area,
        "subject_value_per_sqft": subject_value_per_sqft,
        "comps": comps,
        "median_comp_value_per_sqft": median_comp_value_per_sqft,
        "equity_gap_per_sqft": equity_gap_per_sqft,
        "estimated_savings": estimated_savings,
        "comps_below_subject": comps_below_subject,
        "qualifying_comp_count": len(qualifying_ppsf),
        "ppsf_distribution_chart": _ppsf_distribution_chart(
            qualifying_ppsf, subject_value_per_sqft
        ),
        "min_score": min_score,
        "tax_impact": tax_impact,
    }

    return render(request, "brazos_protest_analysis.html", context)
