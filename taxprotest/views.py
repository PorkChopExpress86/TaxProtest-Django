# home/views.py

import csv
import statistics
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

import redis
from django.conf import settings
from django.core.paginator import Paginator
from django.db import connection
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.urls import reverse

from data.assessment_history import evaluate_cap_status
from data.comparables import (
    COUNTY_CHOICES,
    ComparableProperty,
    resolve_source,
    search_comparables,
)
from data.models import (
    AssessmentHistory,
    BuildingDetail,
    ExtraFeature,
    PropertyAccount,
    PropertyRecord,
)
from data.similarity import find_similar_properties, format_feature_list, get_similarity_label
from data.tax_impact import calculate_tax_impact

EXPORT_CSV_MAX_ROWS = 1000
EXPORT_MIN_TEXT_FILTER_LENGTH = 3
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
SIMILAR_DEFAULT_MAX_DISTANCE = 10.0
SIMILAR_MIN_MAX_DISTANCE = 0.1
SIMILAR_MAX_MAX_DISTANCE = 50.0
SIMILAR_DEFAULT_MAX_RESULTS = 20
SIMILAR_MIN_MAX_RESULTS = 1
SIMILAR_MAX_MAX_RESULTS = 100
SIMILAR_DEFAULT_MIN_SCORE = 30.0
SIMILAR_MIN_MIN_SCORE = 0.0
SIMILAR_MAX_MIN_SCORE = 100.0
ONE_HUNDRED = Decimal("100")
PERCENT = Decimal("0.01")


def _has_meaningful_export_filter(params):
    zip_code = params.get("zip_code", "").strip()
    if len(zip_code) == 5 and zip_code.isdigit():
        return True

    for field in ("first_name", "last_name", "address", "street_name"):
        value = params.get(field, "")
        if len("".join(str(value).split())) >= EXPORT_MIN_TEXT_FILTER_LENGTH:
            return True

    return False


def _csv_safe_text(value):
    text = str(value or "")
    if text.startswith(CSV_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def _clamped_float_param(value, default, lower, upper):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(upper, parsed))


def _clamped_int_param(value, default, lower, upper):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(upper, parsed))


def _active_related_maps(properties):
    account_numbers = [prop.account_number for prop in properties]

    buildings_by_account = {}
    for building in BuildingDetail.objects.filter(
        account_number__in=account_numbers,
        is_active=True,
    ).order_by("id"):
        buildings_by_account.setdefault(building.account_number, building)

    features_by_account = defaultdict(list)
    for feature in ExtraFeature.objects.filter(
        account_number__in=account_numbers,
        is_active=True,
    ).order_by("feature_description", "feature_code", "id"):
        features_by_account[feature.account_number].append(feature)

    return buildings_by_account, features_by_account


def _comparable_row(
    comparable: ComparableProperty,
    *,
    distance: float | None = None,
    similarity_score: float | None = None,
    match_label: str = "",
    score_breakdown: list | None = None,
    is_target: bool = False,
) -> dict[str, object]:
    """Flatten a comparable into the row shape the templates render.

    One shape for every county: fields a district does not publish (Brazos has no
    bedroom, bathroom or condition data) come through as None and the templates
    show a dash, rather than each county needing its own table.
    """
    row: dict[str, object] = {
        "account_number": comparable.key,
        "county": comparable.county,
        "county_label": comparable.county_label,
        "owner_name": comparable.owner_name,
        "address": comparable.street_number,
        "street_name": comparable.street_name,
        "zip_code": comparable.zipcode,
        "assessed_value": comparable.assessed_value,
        "building_area": comparable.living_area,
        "land_area": comparable.land_area,
        "ppsf": comparable.price_per_sqft,
        "bedrooms": comparable.bedrooms,
        "bathrooms": comparable.bathrooms,
        "quality_code": comparable.quality_code or None,
        "year_built": comparable.effective_year,
        "features": format_feature_list(list(comparable.features), max_features=5),
    }
    if distance is not None:
        row["distance"] = distance
    if similarity_score is not None:
        row["similarity_score"] = similarity_score
        row["match_label"] = match_label or get_similarity_label(float(similarity_score))
        row["score_breakdown"] = score_breakdown or []
        row["is_target"] = is_target
    return row


def _assessment_history_rows(prop: PropertyRecord, limit: int = 5) -> list[dict[str, object]]:
    history = list(
        AssessmentHistory.objects.filter(account_number=prop.account_number).order_by("-tax_year")[
            :limit
        ]
    )
    rows = []
    for index, entry in enumerate(history):
        prior = history[index + 1] if index + 1 < len(history) else None
        increase_percent = None
        if entry.assessed_value is not None and prior and prior.assessed_value:
            increase_percent = (
                (entry.assessed_value - prior.assessed_value) / prior.assessed_value * ONE_HUNDRED
            ).quantize(PERCENT, rounding=ROUND_HALF_UP)
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


def _brazos_assessment_history(prop_id: int, limit: int = 5) -> list[dict[str, object]]:
    """Assessment history assembled from Brazos' per-year account rows.

    Brazos has no AssessmentHistory table; each certified roll year is its own
    PropertyAccount row, so the history is however many years have been loaded.
    """
    accounts = list(PropertyAccount.objects.filter(prop_id=prop_id).order_by("-tax_year")[:limit])
    rows: list[dict[str, object]] = []
    for index, account in enumerate(accounts):
        prior = accounts[index + 1] if index + 1 < len(accounts) else None
        assessed = account.assessed_value
        increase_percent = None
        if assessed is not None and prior is not None and prior.assessed_value:
            increase_percent = (
                (assessed - prior.assessed_value) / prior.assessed_value * ONE_HUNDRED
            ).quantize(PERCENT, rounding=ROUND_HALF_UP)
        rows.append(
            {
                "tax_year": account.tax_year,
                "assessed_value": assessed,
                "appraised_value": account.appraised_value,
                "market_value": account.market_value,
                "increase_percent": increase_percent,
                "cap_status": _brazos_cap_status(account, increase_percent),
            }
        )
    return rows


def _brazos_cap_status(account: PropertyAccount, increase_percent) -> dict[str, object]:
    """Cap status in the same shape `evaluate_cap_status` returns.

    Brazos states the limitation directly as an amount rather than leaving it to
    be inferred: `ten_percent_cap` for a homestead, `circuit_breaker_val` for the
    SB2 limitation. A non-zero amount means a cap was applied that year.
    """
    homestead = bool(account.hs_exempt)
    cap_type = "homestead" if homestead else "circuit_breaker"
    limit_percent = Decimal("10") if homestead else Decimal("20")
    applied = account.ten_percent_cap or Decimal("0")
    breaker = account.circuit_breaker_val or Decimal("0")
    overage = applied + breaker

    if account.assessed_value is None:
        return {
            "status": "unknown",
            "label": "Needs review",
            "cap_type": cap_type,
            "limit_percent": limit_percent,
            "increase_percent": increase_percent,
            "allowed_value": None,
            "overage": None,
        }

    limited = overage > 0
    return {
        # A cap that actually bit means the raw value exceeded the limit.
        "status": "over_limit" if limited else "within_limit",
        "label": "Capped" if limited else "Within cap",
        "cap_type": cap_type,
        "limit_percent": limit_percent,
        "increase_percent": increase_percent,
        "allowed_value": account.assessed_value,
        "overage": overage if limited else None,
    }


def _history_for(comparable: ComparableProperty, limit: int = 5) -> list[dict[str, object]]:
    """Assessment history for whichever county the property belongs to."""
    if comparable.county == "brazos":
        return _brazos_assessment_history(int(comparable.key), limit=limit)
    return _assessment_history_rows(comparable.source, limit=limit)  # type: ignore[arg-type]


def _score_breakdown_summary(components: list[dict[str, object]]) -> str:
    parts = []
    for component in components:
        if component.get("points") is None:
            continue
        parts.append(f"{component['label']}: {component['points']}/{component['weight']}")
    return "; ".join(parts)


def _assessment_history_chart(rows: list[dict[str, object]]) -> dict[str, object] | None:
    values = [
        (int(row["tax_year"]), float(row["assessed_value"]))
        for row in rows
        if row.get("assessed_value") is not None
    ]
    if not values:
        return None

    values.sort(key=lambda item: item[0])
    width = 520.0
    height = 180.0
    left = 40.0
    right = 16.0
    top = 16.0
    bottom = 28.0
    plot_width = width - left - right
    plot_height = height - top - bottom

    years = [year for year, _ in values]
    amounts = [amount for _, amount in values]
    min_amount = min(amounts)
    max_amount = max(amounts)
    amount_span = max(max_amount - min_amount, 1.0)
    year_span = max(len(values) - 1, 1)

    points = []
    for index, (year, amount) in enumerate(values):
        x = left + (plot_width * index / year_span)
        y = top + plot_height - (((amount - min_amount) / amount_span) * plot_height)
        points.append(
            {
                "year": year,
                "amount": amount,
                "x": round(x, 2),
                "y": round(y, 2),
            }
        )

    if len(points) == 1:
        path = f"M {points[0]['x']} {points[0]['y']}"
    else:
        path = "M " + " L ".join(f"{point['x']} {point['y']}" for point in points)

    y_ticks = []
    for idx in range(3):
        ratio = idx / 2
        amount = max_amount - (amount_span * ratio)
        y = top + (plot_height * ratio)
        y_ticks.append({"amount": amount, "y": round(y, 2)})

    return {
        "width": round(width),
        "height": round(height),
        "path": path,
        "points": points,
        "y_ticks": y_ticks,
        "baseline_y": round(top + plot_height, 2),
        "left": round(left, 2),
        "right": round(width - right, 2),
    }


def _ppsf_distribution_chart(
    comp_values: list[float], subject_value: float | None, bins: int = 10
) -> dict[str, object] | None:
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
    average_ratio = (average_value - min_value) / (max_value - min_value)
    average_ratio = max(0.0, min(1.0, average_ratio))
    average_x = round(average_ratio * width, 2)

    subject_x = None
    if subject_value is not None:
        ratio = (subject_value - min_value) / (max_value - min_value)
        ratio = max(0.0, min(1.0, ratio))
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


def index(request):
    results = []
    page_obj = None

    query_source = request.GET if request.method == "GET" else request.POST

    first_name = query_source.get("first_name", "").strip()
    last_name = query_source.get("last_name", "").strip()
    address = query_source.get("address", "").strip()
    street_name = query_source.get("street_name", "").strip()
    zip_code = query_source.get("zip_code", "").strip()
    page_number = query_source.get("page", "1")
    sort = query_source.get("sort", "zipcode")
    direction = query_source.get("dir", "asc")
    county = query_source.get("county", "").strip()
    if county not in {choice for choice, _ in COUNTY_CHOICES}:
        county = ""

    filters_applied = any([first_name, last_name, address, street_name, zip_code])

    params = {
        "first_name": first_name,
        "last_name": last_name,
        "address": address,
        "street_name": street_name,
        "zip_code": zip_code,
        "sort": sort,
        "dir": direction,
        "county": county,
    }

    if filters_applied:
        # Searching every county returns a combined, per-county-capped list, so
        # pagination happens over that list rather than a single queryset.
        comparables = search_comparables(params, county or None)
        paginator = Paginator(comparables, 200)
        page_obj = paginator.get_page(page_number)
        results = [_comparable_row(c) for c in page_obj.object_list]

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
        "county": county,
        "county_choices": COUNTY_CHOICES,
    }

    return render(request, "index.html", context)


def export_csv(request):
    """Export all search results to CSV."""
    first_name = request.GET.get("first_name", "").strip()
    last_name = request.GET.get("last_name", "").strip()
    address = request.GET.get("address", "").strip()
    street_name = request.GET.get("street_name", "").strip()
    zip_code = request.GET.get("zip_code", "").strip()
    sort = request.GET.get("sort", "zipcode")
    direction = request.GET.get("dir", "asc")
    county = request.GET.get("county", "").strip()
    if county not in {choice for choice, _ in COUNTY_CHOICES}:
        county = ""

    params = {
        "first_name": first_name,
        "last_name": last_name,
        "address": address,
        "street_name": street_name,
        "zip_code": zip_code,
        "sort": sort,
        "dir": direction,
        "county": county,
    }

    if not _has_meaningful_export_filter(params):
        return HttpResponseBadRequest(
            "Export requires meaningful search criteria: a 5-digit ZIP code or at least "
            f"{EXPORT_MIN_TEXT_FILTER_LENGTH} non-space characters in a text filter."
        )

    comparables = search_comparables(params, county or None, limit=EXPORT_CSV_MAX_ROWS)[
        :EXPORT_CSV_MAX_ROWS
    ]

    # Create CSV response
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="property_search.csv"'

    writer = csv.writer(response)
    # County is appended rather than inserted, so existing column positions —
    # and anything already consuming this export — stay valid.
    writer.writerow(
        [
            "Account Number",
            "Owner Name",
            "Street Number",
            "Street Name",
            "Zip Code",
            "Assessed Value",
            "Building Area (sqft)",
            "Bedrooms",
            "Bathrooms",
            "Quality",
            "Features",
            "Price per sqft",
            "County",
        ]
    )

    for comparable in comparables:
        bathrooms = f"{float(comparable.bathrooms):.1f}" if comparable.bathrooms is not None else ""
        features_text = format_feature_list(list(comparable.features), max_features=10)
        ppsf = comparable.price_per_sqft

        writer.writerow(
            [
                _csv_safe_text(comparable.key),
                _csv_safe_text(comparable.owner_name),
                _csv_safe_text(comparable.street_number),
                _csv_safe_text(comparable.street_name),
                _csv_safe_text(comparable.zipcode),
                comparable.assessed_value if comparable.assessed_value else "",
                comparable.living_area if comparable.living_area else "",
                comparable.bedrooms if comparable.bedrooms is not None else "",
                bathrooms,
                _csv_safe_text(comparable.quality_code),
                _csv_safe_text(features_text if features_text != "None" else ""),
                f"{ppsf:.2f}" if ppsf else "",
                _csv_safe_text(comparable.county_label),
            ]
        )

    return response


def similar_properties(request, account_number):
    """Find and display properties similar to the given account, in any county."""
    county = request.GET.get("county", "").strip() or None
    resolved = resolve_source(account_number, county)

    if resolved is None:
        return render(
            request,
            "similar_properties.html",
            {"error": "Property not found", "account_number": account_number},
        )

    provider, target = resolved
    target_property = target.source
    target_building = target.building
    target_features = list(target.features)

    # Check if property has required data
    if not target.has_location:
        return render(
            request,
            "similar_properties.html",
            {
                "error": "This property does not have location data required for similarity search.",
                "target_property": target_property,
                "target_building": target_building,
                "target_comparable": target,
                "county_label": target.county_label,
            },
        )

    # Get bounded search parameters
    max_distance = _clamped_float_param(
        request.GET.get("max_distance"),
        SIMILAR_DEFAULT_MAX_DISTANCE,
        SIMILAR_MIN_MAX_DISTANCE,
        SIMILAR_MAX_MAX_DISTANCE,
    )
    max_results = _clamped_int_param(
        request.GET.get("max_results"),
        SIMILAR_DEFAULT_MAX_RESULTS,
        SIMILAR_MIN_MAX_RESULTS,
        SIMILAR_MAX_MAX_RESULTS,
    )
    min_score = _clamped_float_param(
        request.GET.get("min_score"),
        SIMILAR_DEFAULT_MIN_SCORE,
        SIMILAR_MIN_MIN_SCORE,
        SIMILAR_MAX_MIN_SCORE,
    )

    # Find similar properties
    similar = find_similar_properties(
        account_number=account_number,
        max_distance_miles=max_distance,
        max_results=max_results,
        min_score=min_score,
        source=provider.name,
    )

    # Format results for template, subject property first
    formatted_results = [
        _comparable_row(
            target,
            distance=0.0,
            similarity_score=100,
            match_label="Your property",
            is_target=True,
        )
    ]
    target_ppsf = target.price_per_sqft

    # Then add similar properties
    for result in similar:
        formatted_results.append(
            _comparable_row(
                result["comparable"],
                distance=result["distance"],
                similarity_score=result["similarity_score"],
                score_breakdown=result.get("score_breakdown", []),
            )
        )

    # Calculate percentile for target property's price per sqft
    ppsf_values = [r["ppsf"] for r in formatted_results if r["ppsf"] is not None]
    target_ppsf_percentile = None
    if target_ppsf and ppsf_values:
        ppsf_values_sorted = sorted(ppsf_values)
        target_position = sum(1 for v in ppsf_values_sorted if v <= target_ppsf)
        target_ppsf_percentile = (target_position / len(ppsf_values_sorted)) * 100

    # Sort comparable properties by match quality (target always first)
    target_entry = next((r for r in formatted_results if r.get("is_target")), None)
    comparable_entries = [r for r in formatted_results if not r.get("is_target")]

    def ppsf_sort_key(entry):
        value = entry.get("ppsf")
        return float(value) if value is not None else float("inf")

    comparable_entries.sort(
        key=lambda entry: (
            -float(entry.get("similarity_score") or 0),
            float(entry.get("distance") or 0),
            ppsf_sort_key(entry),
            entry.get("account_number") or "",
        )
    )

    if target_entry:
        formatted_results = [target_entry] + comparable_entries
    else:
        formatted_results = comparable_entries

    # Calculate protest recommendation based on PPSF comparison
    protest_recommendation = None
    protest_recommendation_reason = None
    protest_recommendation_level = None
    ppsf_median = None
    ppsf_average = None
    ppsf_min = None
    ppsf_max = None
    comparable_count = 0
    comparable_avg_score = None

    # Only calculate if target has valid PPSF
    if target_ppsf and comparable_entries:
        # Extract PPSF values from comparables only (exclude target)
        comparable_ppsf_data = [
            {"ppsf": r["ppsf"], "score": r["similarity_score"]}
            for r in comparable_entries
            if r.get("ppsf") is not None and r.get("similarity_score") is not None
        ]

        # Require at least 3 valid comparables
        if len(comparable_ppsf_data) >= 3:
            comparable_ppsf_values = [d["ppsf"] for d in comparable_ppsf_data]
            comparable_ppsf_values_sorted = sorted(comparable_ppsf_values)
            comparable_count = len(comparable_ppsf_values)

            # Calculate median
            mid = comparable_count // 2
            if comparable_count % 2 == 1:
                ppsf_median = comparable_ppsf_values_sorted[mid]
            else:
                ppsf_median = (
                    comparable_ppsf_values_sorted[mid - 1] + comparable_ppsf_values_sorted[mid]
                ) / 2.0

            # Calculate average
            ppsf_average = sum(comparable_ppsf_values) / comparable_count

            # Calculate range
            ppsf_min = comparable_ppsf_values_sorted[0]
            ppsf_max = comparable_ppsf_values_sorted[-1]

            # Calculate average similarity score
            comparable_scores = [d["score"] for d in comparable_ppsf_data]
            comparable_avg_score = sum(comparable_scores) / len(comparable_scores)

            # Calculate percentage difference from median
            over_percentage = (
                (float(target_ppsf) - float(ppsf_median)) / float(ppsf_median)
            ) * 100.0

            # Generate recommendation based on thresholds
            if over_percentage >= 20:
                protest_recommendation_level = "strong"
                protest_recommendation = "Recommend protesting"
                protest_recommendation_reason = (
                    f"Your price per sqft (${target_ppsf:.2f}) is about {over_percentage:.0f}% above "
                    f"the median (${ppsf_median:.2f}) of {comparable_count} similar properties "
                    f"(avg match score {comparable_avg_score:.0f})."
                )
            elif over_percentage >= 10:
                protest_recommendation_level = "moderate"
                protest_recommendation = "Consider protesting"
                protest_recommendation_reason = (
                    f"Your price per sqft (${target_ppsf:.2f}) is about {over_percentage:.0f}% above "
                    f"the median (${ppsf_median:.2f}) of {comparable_count} similar properties "
                    f"(avg match score {comparable_avg_score:.0f})."
                )
            elif over_percentage <= -10:
                protest_recommendation_level = "low"
                protest_recommendation = "Protest not recommended"
                protest_recommendation_reason = (
                    f"Your price per sqft (${target_ppsf:.2f}) is about {abs(over_percentage):.0f}% below "
                    f"the median (${ppsf_median:.2f}) of {comparable_count} similar properties."
                )
            else:
                protest_recommendation_level = "neutral"
                protest_recommendation = "Borderline – depends on other factors"
                protest_recommendation_reason = (
                    f"Your price per sqft (${target_ppsf:.2f}) is close to the median (${ppsf_median:.2f}) "
                    f"of {comparable_count} similar properties."
                )

    assessment_history = _history_for(target)

    context = {
        "target_property": target_property,
        "target_building": target_building,
        "target_comparable": target,
        "county": target.county,
        "county_label": target.county_label,
        "target_features": format_feature_list(target_features),
        "assessment_history": assessment_history,
        "assessment_history_chart": _assessment_history_chart(assessment_history),
        "target_year_built": target.effective_year,
        "target_bedrooms": target.bedrooms,
        "target_bathrooms": target.bathrooms,
        "target_quality_code": target.quality_code or None,
        "target_area": target.living_area,
        "target_ppsf": target_ppsf,
        "target_ppsf_percentile": target_ppsf_percentile,
        "results": formatted_results,
        "results_sort_label": "match score (best match first)",
        "max_distance": max_distance,
        "max_results": max_results,
        "min_score": min_score,
        # Protest recommendation fields
        "protest_recommendation": protest_recommendation,
        "protest_recommendation_reason": protest_recommendation_reason,
        "protest_recommendation_level": protest_recommendation_level,
        "ppsf_median": ppsf_median,
        "ppsf_average": ppsf_average,
        "ppsf_min": ppsf_min,
        "ppsf_max": ppsf_max,
        "comparable_count": comparable_count,
        "comparable_avg_score": comparable_avg_score,
    }

    return render(request, "similar_properties.html", context)


## Removed mock results function; now using real data


def protest_analysis(request, account_number):
    """Protest analysis page: equity comparison for ARB hearing preparation."""
    county = request.GET.get("county", "").strip() or None
    resolved = resolve_source(account_number, county)
    if resolved is None:
        raise Http404("Property not found")

    provider, target = resolved
    target_property = target.source
    target_building = target.building
    target_features = list(target.features)

    if not target.has_location:
        return render(
            request,
            "protest_analysis.html",
            {
                "error": "This property does not have location data required for similarity search.",
                "target_property": target_property,
                "target_building": target_building,
                "target_comparable": target,
                "county": target.county,
                "county_label": target.county_label,
            },
        )

    # Parse and clamp min_score to [52.0, 100.0]; default 70.0
    try:
        min_score = float(request.GET.get("min_score", "70.0"))
    except (ValueError, TypeError):
        min_score = 70.0
    min_score = max(52.0, min(100.0, min_score))

    # Compute subject $/sqft
    subject_heat_area = float(target.living_area) if target.living_area else None
    subject_value_per_sqft = target.price_per_sqft

    # Find similar properties
    similar = find_similar_properties(
        account_number=account_number,
        max_distance_miles=10.0,
        max_results=50,
        min_score=min_score,
        source=provider.name,
    )

    # Build enriched comp list
    comps = []
    for result in similar:
        comparable = result["comparable"]
        comp_value_per_sqft = comparable.price_per_sqft
        comp_delta = None
        if comp_value_per_sqft is not None and subject_value_per_sqft is not None:
            comp_delta = comp_value_per_sqft - subject_value_per_sqft

        comps.append(
            {
                **_comparable_row(
                    comparable,
                    distance=result["distance"],
                    similarity_score=result["similarity_score"],
                    score_breakdown=result.get("score_breakdown", []),
                ),
                "heat_area": float(comparable.living_area) if comparable.living_area else None,
                "comp_value_per_sqft": comp_value_per_sqft,
                "comp_delta": comp_delta,
                "condition_code": comparable.condition_code or None,
                "score_breakdown_summary": _score_breakdown_summary(
                    result.get("score_breakdown", [])
                ),
            }
        )

    # Compute equity summary
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
        if subject_heat_area:
            estimated_savings = max(0.0, equity_gap_per_sqft * subject_heat_area)
        comps_below_subject = sum(1 for p in qualifying_ppsf if p < subject_value_per_sqft)

    assessment_history = _history_for(target)
    median_assessed_value = None
    if median_comp_value_per_sqft is not None and subject_heat_area:
        median_assessed_value = Decimal(str(median_comp_value_per_sqft)) * Decimal(
            str(subject_heat_area)
        )
    # Tax impact needs TaxUnitRate and PropertyJurisdictionExemption rows, which
    # are only loaded for Harris. It degrades to completeness="missing" elsewhere.
    tax_impact = calculate_tax_impact(
        account_number=target.key,
        tax_year=assessment_history[0]["tax_year"] if assessment_history else None,
        median_assessed_value=median_assessed_value,
    )

    context = {
        "target_property": target_property,
        "target_building": target_building,
        "target_comparable": target,
        "county": target.county,
        "county_label": target.county_label,
        "target_features": format_feature_list(target_features),
        "assessment_history": assessment_history,
        "assessment_history_chart": _assessment_history_chart(assessment_history),
        "subject_heat_area": subject_heat_area,
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
        "pdf_export_url": reverse("protest_analysis_pdf", args=[target.key]),
        "tax_impact": tax_impact,
    }

    return render(request, "protest_analysis.html", context)


def protest_analysis_export(request, account_number):
    """CSV export of protest analysis comparable properties."""
    resolved = resolve_source(account_number, request.GET.get("county", "").strip() or None)
    if resolved is None:
        raise Http404("Property not found")
    provider, target = resolved
    target_property = target.source
    target_building = target.building

    try:
        min_score = float(request.GET.get("min_score", "70.0"))
    except (ValueError, TypeError):
        min_score = 70.0
    min_score = max(52.0, min(100.0, min_score))

    subject_heat_area = float(target.living_area) if target.living_area else None
    subject_value_per_sqft = target.price_per_sqft

    similar = find_similar_properties(
        account_number=account_number,
        max_distance_miles=10.0,
        max_results=50,
        min_score=min_score,
        source=provider.name,
    )

    response = HttpResponse(content_type="text/csv")
    safe_account = account_number.replace('"', "").replace("\\", "")
    response["Content-Disposition"] = f'attachment; filename="protest_analysis_{safe_account}.csv"'

    writer = csv.writer(response)
    qualifying_ppsf = [
        result["comparable"].price_per_sqft
        for result in similar
        if result["comparable"].price_per_sqft is not None
    ]

    median_assessed_value = None
    if subject_heat_area and qualifying_ppsf:
        median_comp_ppsf = statistics.median(qualifying_ppsf)
        median_assessed_value = Decimal(str(median_comp_ppsf)) * Decimal(str(subject_heat_area))
    tax_impact = calculate_tax_impact(
        account_number=target.key,
        tax_year=None,
        median_assessed_value=median_assessed_value,
    )

    writer.writerow(
        [
            "address",
            "similarity_score",
            "similarity_label",
            "living_area_sqft",
            "bedrooms",
            "bathrooms",
            "year_built",
            "quality_code",
            "condition_code",
            "assessed_value",
            "value_per_sqft",
            "delta_vs_subject_per_sqft",
            "score_breakdown",
            "tax_year_used",
            "tax_impact_completeness",
            "current_tax_owed",
            "median_tax_owed",
            "estimated_tax_savings",
            "tax_impact_warnings",
        ]
    )

    for result in similar:
        comparable = result["comparable"]
        comp_assessed = comparable.assessed_value
        comp_heat_area = float(comparable.living_area) if comparable.living_area else None
        comp_value_per_sqft = comparable.price_per_sqft
        comp_delta = None
        if comp_value_per_sqft is not None and subject_value_per_sqft is not None:
            comp_delta = comp_value_per_sqft - subject_value_per_sqft

        writer.writerow(
            [
                comparable.full_address,
                f"{result['similarity_score']:.1f}",
                get_similarity_label(result["similarity_score"]),
                f"{comp_heat_area:.0f}" if comp_heat_area else "",
                comparable.bedrooms if comparable.bedrooms is not None else "",
                f"{float(comparable.bathrooms):.1f}" if comparable.bathrooms is not None else "",
                comparable.effective_year or "",
                comparable.quality_code,
                comparable.condition_code,
                f"{float(comp_assessed):.2f}" if comp_assessed else "",
                f"{comp_value_per_sqft:.2f}" if comp_value_per_sqft is not None else "",
                f"{comp_delta:.2f}" if comp_delta is not None else "",
                _score_breakdown_summary(result.get("score_breakdown", [])),
                tax_impact.tax_year or "",
                tax_impact.completeness,
                f"{float(tax_impact.current_tax_owed):.2f}",
                f"{float(tax_impact.median_tax_owed):.2f}",
                f"{float(tax_impact.estimated_savings):.2f}",
                " | ".join(tax_impact.warnings),
            ]
        )

    return response


def _pdf_escape(text):
    return str(text or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _simple_pdf(lines):
    text_commands = ["BT", "/F1 12 Tf", "72 760 Td"]
    for index, line in enumerate(lines):
        if index:
            text_commands.append("0 -18 Td")
        text_commands.append(f"({_pdf_escape(line)}) Tj")
    text_commands.append("ET")
    stream = "\n".join(text_commands).encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(payload)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def protest_analysis_pdf(request, account_number):
    """PDF export of the protest evidence report."""
    resolved = resolve_source(account_number, request.GET.get("county", "").strip() or None)
    if resolved is None:
        raise Http404("Property not found")
    provider, target = resolved
    target_property = target.source
    target_building = target.building
    assessed = target.assessed_value
    try:
        min_score = float(request.GET.get("min_score", "70.0"))
    except (ValueError, TypeError):
        min_score = 70.0
    min_score = max(52.0, min(100.0, min_score))

    lines = [
        # The county is stated, not assumed — this document is filed as evidence.
        f"{target.county_label} County Property Tax Protest Evidence Report",
        f"Account: {target.key}",
        f"Property: {target.full_address}",
        f"Assessed Value: ${float(assessed):,.0f}" if assessed else "Assessed Value: unavailable",
    ]
    if target.living_area:
        lines.append(f"Living Area: {float(target.living_area):,.0f} sqft")
        if target.price_per_sqft is not None:
            lines.append(f"Subject Value/Sqft: ${target.price_per_sqft:,.2f}")

    history_rows = _history_for(target)
    if history_rows:
        lines.append("")
        lines.append("Assessment History")
        for row in history_rows:
            assessed_text = (
                f"${float(row['assessed_value']):,.0f}" if row.get("assessed_value") else "-"
            )
            change_text = (
                f"{row['increase_percent']}%" if row.get("increase_percent") is not None else "-"
            )
            cap_status = row["cap_status"]["label"] if row.get("cap_status") else "Needs review"
            lines.append(f"{row['tax_year']}: {assessed_text}, YoY {change_text}, {cap_status}")

    similar = find_similar_properties(
        account_number=account_number,
        max_distance_miles=10.0,
        max_results=10,
        min_score=min_score,
        source=provider.name,
    )
    if similar:
        lines.append("")
        lines.append("Comparable Evidence")
        for result in similar:
            comparable = result["comparable"]
            comp_ppsf = comparable.price_per_sqft
            ppsf_text = f", ${comp_ppsf:,.2f}/sqft" if comp_ppsf is not None else ""
            label = comparable.full_address or f"Account {comparable.key}"
            lines.append(f"{label}: score {float(result['similarity_score']):.1f}{ppsf_text}")

    median_assessed_value = None
    if target.living_area and similar:
        qualifying_ppsf = [
            result["comparable"].price_per_sqft
            for result in similar
            if result["comparable"].price_per_sqft is not None
        ]
        if qualifying_ppsf:
            median_assessed_value = Decimal(str(statistics.median(qualifying_ppsf))) * Decimal(
                str(float(target.living_area))
            )

    tax_impact = calculate_tax_impact(
        account_number=target.key,
        tax_year=history_rows[0]["tax_year"] if history_rows else None,
        median_assessed_value=median_assessed_value,
    )
    lines.extend(
        [
            "",
            "Tax Impact (Estimated)",
            f"Tax Year Used: {tax_impact.tax_year or '-'} ({tax_impact.completeness})",
            f"Current Taxes Owed: ${float(tax_impact.current_tax_owed):,.2f}",
            f"Median-Scenario Taxes Owed: ${float(tax_impact.median_tax_owed):,.2f}",
            f"Estimated Annual Savings: ${float(tax_impact.estimated_savings):,.2f}",
        ]
    )
    if tax_impact.warnings:
        lines.append(f"Warnings: {' | '.join(tax_impact.warnings)}")

    response = HttpResponse(_simple_pdf(lines), content_type="application/pdf")
    safe_account = account_number.replace('"', "").replace("\\", "")
    response["Content-Disposition"] = f'attachment; filename="protest_analysis_{safe_account}.pdf"'
    return response


def about(request):
    return render(request, "about.html")


def healthz(request):
    """Return 200 if the app can reach the database."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - defensive
        return JsonResponse({"status": "error", "detail": str(exc)}, status=503)

    return JsonResponse({"status": "ok"})


def readiness(request):
    """Return readiness status including Redis availability."""
    payload = {"database": "ok", "redis": "ok"}
    status_code = 200

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:
        payload["database"] = "error"
        payload["detail_database"] = str(exc)
        status_code = 503

    try:
        client = redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=1)
        client.ping()
        client.close()
    except Exception as exc:  # pragma: no cover - depends on runtime redis
        payload["redis"] = "error"
        payload["detail_redis"] = str(exc)
        status_code = 503

    return JsonResponse(payload, status=status_code)
