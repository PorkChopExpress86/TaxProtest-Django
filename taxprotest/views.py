# home/views.py

import csv
import statistics
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

import redis
from django.conf import settings
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import connection
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.urls import reverse

from .forms import ContactForm

from data.assessment_history import evaluate_cap_status
from data.models import AssessmentHistory, BuildingDetail, ExtraFeature, PropertyRecord
from data.query import build_property_search_queryset
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

    filters_applied = any([first_name, last_name, address, street_name, zip_code])

    params = {
        "first_name": first_name,
        "last_name": last_name,
        "address": address,
        "street_name": street_name,
        "zip_code": zip_code,
        "sort": sort,
        "dir": direction,
    }

    if filters_applied:
        qs = build_property_search_queryset(params)

        paginator = Paginator(qs, 200)
        page_obj = paginator.get_page(page_number)
        properties = list(page_obj.object_list)
        buildings_by_account, features_by_account = _active_related_maps(properties)

        formatted = []
        for prop in properties:
            assessed = prop.assessed_value or prop.value
            bldg_area = prop.building_area or 0
            ppsf = None
            if assessed and bldg_area and bldg_area > 0:
                try:
                    ppsf = float(assessed) / float(bldg_area)
                except Exception:
                    ppsf = None

            # Get building details (bedrooms, bathrooms, quality)
            building = buildings_by_account.get(prop.account_number)
            bedrooms = building.bedrooms if building else None
            bathrooms = building.bathrooms if building else None
            quality_code = building.quality_code if building else None

            # Get extra features (pool, garage, etc.)
            features = features_by_account.get(prop.account_number, [])
            features_text = format_feature_list(features, max_features=5) if features else None

            formatted.append(
                {
                    "account_number": prop.account_number,
                    "owner_name": prop.owner_name,
                    "address": prop.street_number,
                    "street_name": prop.street_name,
                    "zip_code": prop.zipcode,
                    "assessed_value": assessed,
                    "building_area": prop.building_area,
                    "land_area": prop.land_area,
                    "ppsf": ppsf,
                    "bedrooms": bedrooms,
                    "bathrooms": bathrooms,
                    "quality_code": quality_code,
                    "features": features_text,
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

    params = {
        "first_name": first_name,
        "last_name": last_name,
        "address": address,
        "street_name": street_name,
        "zip_code": zip_code,
        "sort": sort,
        "dir": direction,
    }

    if not _has_meaningful_export_filter(params):
        return HttpResponseBadRequest(
            "Export requires meaningful search criteria: a 5-digit ZIP code or at least "
            f"{EXPORT_MIN_TEXT_FILTER_LENGTH} non-space characters in a text filter."
        )

    qs = build_property_search_queryset(params)
    properties = list(qs[:EXPORT_CSV_MAX_ROWS])
    buildings_by_account, features_by_account = _active_related_maps(properties)

    # Create CSV response
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="property_search.csv"'

    writer = csv.writer(response)
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
        ]
    )

    for prop in properties:
        assessed = prop.assessed_value or prop.value
        bldg_area = prop.building_area or 0
        ppsf = None
        if assessed and bldg_area and bldg_area > 0:
            try:
                ppsf = float(assessed) / float(bldg_area)
            except Exception:
                ppsf = None

        # Get building details
        building = buildings_by_account.get(prop.account_number)
        bedrooms = building.bedrooms if building else ""
        bathrooms = f"{float(building.bathrooms):.1f}" if building and building.bathrooms else ""
        quality_code = building.quality_code if building else ""

        # Get extra features
        features = features_by_account.get(prop.account_number, [])
        features_text = format_feature_list(features, max_features=10) if features else ""

        writer.writerow(
            [
                _csv_safe_text(prop.account_number),
                _csv_safe_text(prop.owner_name),
                _csv_safe_text(prop.street_number),
                _csv_safe_text(prop.street_name),
                _csv_safe_text(prop.zipcode),
                assessed if assessed else "",
                bldg_area if bldg_area else "",
                bedrooms,
                bathrooms,
                _csv_safe_text(quality_code),
                _csv_safe_text(features_text),
                f"{ppsf:.2f}" if ppsf else "",
            ]
        )

    return response


def similar_properties(request, account_number):
    """Find and display properties similar to the given account."""
    # Use filter().first() to handle potential duplicates
    target_property = PropertyRecord.objects.filter(account_number=account_number).first()

    if not target_property:
        return render(
            request,
            "similar_properties.html",
            {"error": "Property not found", "account_number": account_number},
        )

    # Get target building and features for display
    target_building = target_property.buildings.filter(is_active=True).first()
    target_features = list(target_property.extra_features.filter(is_active=True))

    # Check if property has required data
    if not target_property.latitude or not target_property.longitude:
        return render(
            request,
            "similar_properties.html",
            {
                "error": "This property does not have location data required for similarity search.",
                "target_property": target_property,
                "target_building": target_building,
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
    )

    # Format results for template
    formatted_results = []

    # First, add the target property to the results
    target_assessed = target_property.assessed_value or target_property.value
    target_bldg_area = (
        target_building.heat_area if target_building else (target_property.building_area or 0)
    )
    target_ppsf = None
    if target_assessed and target_bldg_area and target_bldg_area > 0:
        try:
            target_ppsf = float(target_assessed) / float(target_bldg_area)
        except Exception:
            target_ppsf = None

    formatted_results.append(
        {
            "account_number": target_property.account_number,
            "owner_name": target_property.owner_name,
            "address": target_property.street_number,
            "street_name": target_property.street_name,
            "zip_code": target_property.zipcode,
            "assessed_value": target_assessed,
            "building_area": target_bldg_area,
            "land_area": target_property.land_area,
            "ppsf": target_ppsf,
            "distance": 0.0,
            "similarity_score": 100,
            "year_built": target_building.year_built if target_building else None,
            "bedrooms": target_building.bedrooms if target_building else None,
            "bathrooms": target_building.bathrooms if target_building else None,
            "quality_code": target_building.quality_code if target_building else None,
            "features": format_feature_list(target_features, max_features=5),
            "match_label": "Your property",
            "score_breakdown": [],
            "is_target": True,
        }
    )

    # Then add similar properties
    for result in similar:
        prop = result["property"]
        building = result["building"]
        features = result["features"]

        assessed = prop.assessed_value or prop.value
        bldg_area = building.heat_area if building else (prop.building_area or 0)
        ppsf = None
        if assessed and bldg_area and bldg_area > 0:
            try:
                ppsf = float(assessed) / float(bldg_area)
            except Exception:
                ppsf = None

        formatted_results.append(
            {
                "account_number": prop.account_number,
                "owner_name": prop.owner_name,
                "address": prop.street_number,
                "street_name": prop.street_name,
                "zip_code": prop.zipcode,
                "assessed_value": assessed,
                "building_area": bldg_area,
                "land_area": prop.land_area,
                "ppsf": ppsf,
                "distance": result["distance"],
                "similarity_score": result["similarity_score"],
                "year_built": building.year_built if building else None,
                "bedrooms": building.bedrooms if building else None,
                "bathrooms": building.bathrooms if building else None,
                "quality_code": building.quality_code if building else None,
                "features": format_feature_list(features, max_features=5),
                "match_label": get_similarity_label(result["similarity_score"]),
                "score_breakdown": result.get("score_breakdown", []),
                "is_target": False,
            }
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

    assessment_history = _assessment_history_rows(target_property)

    context = {
        "target_property": target_property,
        "target_building": target_building,
        "target_features": format_feature_list(target_features),
        "assessment_history": assessment_history,
        "assessment_history_chart": _assessment_history_chart(assessment_history),
        "target_year_built": target_building.year_built if target_building else None,
        "target_bedrooms": target_building.bedrooms if target_building else None,
        "target_bathrooms": target_building.bathrooms if target_building else None,
        "target_quality_code": (target_building.quality_code if target_building else None),
        "target_area": (
            target_building.heat_area if target_building else target_property.building_area
        ),
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
    target_property = PropertyRecord.objects.filter(account_number=account_number).first()
    if not target_property:
        raise Http404("Property not found")

    target_building = target_property.buildings.filter(is_active=True).first()
    target_features = list(target_property.extra_features.filter(is_active=True))

    if not target_property.latitude or not target_property.longitude:
        return render(
            request,
            "protest_analysis.html",
            {
                "error": "This property does not have location data required for similarity search.",
                "target_property": target_property,
                "target_building": target_building,
            },
        )

    # Parse and clamp min_score to [52.0, 100.0]; default 70.0
    try:
        min_score = float(request.GET.get("min_score", "70.0"))
    except (ValueError, TypeError):
        min_score = 70.0
    min_score = max(52.0, min(100.0, min_score))

    # Compute subject $/sqft
    subject_heat_area = (
        float(target_building.heat_area) if target_building and target_building.heat_area else None
    )
    subject_assessed = target_property.assessed_value or target_property.value
    subject_value_per_sqft = None
    if subject_assessed and subject_heat_area and subject_heat_area > 0:
        try:
            subject_value_per_sqft = float(subject_assessed) / subject_heat_area
        except Exception:
            subject_value_per_sqft = None

    # Find similar properties
    similar = find_similar_properties(
        account_number=account_number,
        max_distance_miles=10.0,
        max_results=50,
        min_score=min_score,
    )

    # Build enriched comp list
    comps = []
    for result in similar:
        prop = result["property"]
        building = result["building"]
        features = result["features"]

        comp_assessed = prop.assessed_value or prop.value
        comp_heat_area = float(building.heat_area) if building and building.heat_area else None

        comp_value_per_sqft = None
        comp_delta = None
        if comp_assessed and comp_heat_area and comp_heat_area > 0:
            try:
                comp_value_per_sqft = float(comp_assessed) / comp_heat_area
                if subject_value_per_sqft is not None:
                    comp_delta = comp_value_per_sqft - subject_value_per_sqft
            except Exception:
                pass

        comps.append(
            {
                "account_number": prop.account_number,
                "address": prop.street_number,
                "street_name": prop.street_name,
                "zip_code": prop.zipcode,
                "assessed_value": comp_assessed,
                "heat_area": comp_heat_area,
                "comp_value_per_sqft": comp_value_per_sqft,
                "comp_delta": comp_delta,
                "distance": result["distance"],
                "similarity_score": result["similarity_score"],
                "match_label": get_similarity_label(result["similarity_score"]),
                "year_built": building.year_built if building else None,
                "bedrooms": building.bedrooms if building else None,
                "bathrooms": building.bathrooms if building else None,
                "quality_code": building.quality_code if building else None,
                "condition_code": building.condition_code if building else None,
                "features": format_feature_list(features, max_features=5),
                "score_breakdown": result.get("score_breakdown", []),
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

    assessment_history = _assessment_history_rows(target_property)
    median_assessed_value = None
    if median_comp_value_per_sqft is not None and subject_heat_area:
        median_assessed_value = Decimal(str(median_comp_value_per_sqft)) * Decimal(
            str(subject_heat_area)
        )
    tax_impact = calculate_tax_impact(
        account_number=target_property.account_number,
        tax_year=assessment_history[0]["tax_year"] if assessment_history else None,
        median_assessed_value=median_assessed_value,
    )

    context = {
        "target_property": target_property,
        "target_building": target_building,
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
        "pdf_export_url": reverse("protest_analysis_pdf", args=[target_property.account_number]),
        "tax_impact": tax_impact,
    }

    return render(request, "protest_analysis.html", context)


def protest_analysis_export(request, account_number):
    """CSV export of protest analysis comparable properties."""
    target_property = PropertyRecord.objects.filter(account_number=account_number).first()
    if not target_property:
        raise Http404("Property not found")

    target_building = target_property.buildings.filter(is_active=True).first()

    try:
        min_score = float(request.GET.get("min_score", "70.0"))
    except (ValueError, TypeError):
        min_score = 70.0
    min_score = max(52.0, min(100.0, min_score))

    subject_heat_area = (
        float(target_building.heat_area) if target_building and target_building.heat_area else None
    )
    subject_assessed = target_property.assessed_value or target_property.value
    subject_value_per_sqft = None
    if subject_assessed and subject_heat_area and subject_heat_area > 0:
        try:
            subject_value_per_sqft = float(subject_assessed) / subject_heat_area
        except Exception:
            pass

    similar = find_similar_properties(
        account_number=account_number,
        max_distance_miles=10.0,
        max_results=50,
        min_score=min_score,
    )

    response = HttpResponse(content_type="text/csv")
    safe_account = account_number.replace('"', "").replace("\\", "")
    response["Content-Disposition"] = f'attachment; filename="protest_analysis_{safe_account}.csv"'

    writer = csv.writer(response)
    qualifying_ppsf = []
    for result in similar:
        prop = result["property"]
        building = result["building"]
        comp_assessed = prop.assessed_value or prop.value
        comp_heat_area = float(building.heat_area) if building and building.heat_area else None
        if comp_assessed and comp_heat_area and comp_heat_area > 0:
            qualifying_ppsf.append(float(comp_assessed) / comp_heat_area)

    median_assessed_value = None
    if subject_heat_area and qualifying_ppsf:
        median_comp_ppsf = statistics.median(qualifying_ppsf)
        median_assessed_value = Decimal(str(median_comp_ppsf)) * Decimal(str(subject_heat_area))
    tax_impact = calculate_tax_impact(
        account_number=target_property.account_number,
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
        prop = result["property"]
        building = result["building"]

        comp_assessed = prop.assessed_value or prop.value
        comp_heat_area = float(building.heat_area) if building and building.heat_area else None

        comp_value_per_sqft = None
        comp_delta = None
        if comp_assessed and comp_heat_area and comp_heat_area > 0:
            try:
                comp_value_per_sqft = float(comp_assessed) / comp_heat_area
                if subject_value_per_sqft is not None:
                    comp_delta = comp_value_per_sqft - subject_value_per_sqft
            except Exception:
                pass

        full_address = f"{prop.street_number} {prop.street_name}".strip()

        writer.writerow(
            [
                full_address,
                f"{result['similarity_score']:.1f}",
                get_similarity_label(result["similarity_score"]),
                f"{comp_heat_area:.0f}" if comp_heat_area else "",
                building.bedrooms if building else "",
                f"{float(building.bathrooms):.1f}" if building and building.bathrooms else "",
                building.year_built if building else "",
                building.quality_code if building else "",
                building.condition_code if building else "",
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
    target_property = PropertyRecord.objects.filter(account_number=account_number).first()
    if not target_property:
        raise Http404("Property not found")

    target_building = target_property.buildings.filter(is_active=True).first()
    assessed = target_property.assessed_value or target_property.value
    try:
        min_score = float(request.GET.get("min_score", "70.0"))
    except (ValueError, TypeError):
        min_score = 70.0
    min_score = max(52.0, min(100.0, min_score))

    lines = [
        "Harris County Property Tax Protest Evidence Report",
        f"Account: {target_property.account_number}",
        f"Property: {target_property.street_number} {target_property.street_name}",
        f"Assessed Value: ${float(assessed):,.0f}" if assessed else "Assessed Value: unavailable",
    ]
    if target_building and target_building.heat_area:
        lines.append(f"Living Area: {float(target_building.heat_area):,.0f} sqft")
        if assessed:
            ppsf = float(assessed) / float(target_building.heat_area)
            lines.append(f"Subject Value/Sqft: ${ppsf:,.2f}")

    history_rows = _assessment_history_rows(target_property)
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
    )
    if similar:
        lines.append("")
        lines.append("Comparable Evidence")
        for result in similar:
            prop = result["property"]
            building = result["building"]
            comp_assessed = prop.assessed_value or prop.value
            comp_ppsf = None
            if comp_assessed and building and building.heat_area:
                comp_ppsf = float(comp_assessed) / float(building.heat_area)
            ppsf_text = f", ${comp_ppsf:,.2f}/sqft" if comp_ppsf is not None else ""
            lines.append(
                f"{prop.street_number} {prop.street_name}: "
                f"score {float(result['similarity_score']):.1f}{ppsf_text}"
            )

    median_assessed_value = None
    if target_building and target_building.heat_area and similar:
        qualifying_ppsf = []
        for result in similar:
            prop = result["property"]
            building = result["building"]
            comp_assessed = prop.assessed_value or prop.value
            if comp_assessed and building and building.heat_area:
                qualifying_ppsf.append(float(comp_assessed) / float(building.heat_area))
        if qualifying_ppsf:
            median_assessed_value = Decimal(str(statistics.median(qualifying_ppsf))) * Decimal(
                str(float(target_building.heat_area))
            )

    tax_impact = calculate_tax_impact(
        account_number=target_property.account_number,
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


def contact(request):
    if request.method == "POST":
        form = ContactForm(request.POST)
        if form.is_valid():
            name = form.cleaned_data["name"]
            sender = form.cleaned_data["email"]
            subject = form.cleaned_data["subject"]
            message = form.cleaned_data["message"]
            body = f"From: {name} <{sender}>\n\n{message}"
            try:
                send_mail(
                    subject=f"[Home Values] {subject}",
                    message=body,
                    from_email=settings.CONTACT_EMAIL,
                    recipient_list=[settings.CONTACT_EMAIL],
                    reply_to=[f"{name} <{sender}>"],
                    fail_silently=False,
                )
                return render(request, "contact.html", {"form": ContactForm(), "sent": True})
            except Exception:
                return render(request, "contact.html", {"form": form, "send_error": True})
    else:
        form = ContactForm()
    return render(request, "contact.html", {"form": form})


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
