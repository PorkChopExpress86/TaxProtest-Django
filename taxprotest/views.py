# home/views.py

import csv

import redis
from django.conf import settings
from django.core.paginator import Paginator
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.http import urlencode

from data.models import PropertyRecord
from data.similarity import find_similar_properties, format_feature_list
from data.query import build_property_search_queryset


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

        formatted = []
        for prop in page_obj.object_list:
            assessed = prop.assessed_value or prop.value
            bldg_area = prop.building_area or 0
            ppsf = None
            if assessed and bldg_area and bldg_area > 0:
                try:
                    ppsf = float(assessed) / float(bldg_area)
                except Exception:
                    ppsf = None

            # Get building details (bedrooms, bathrooms, quality)
            building = prop.buildings.filter(is_active=True).first()
            bedrooms = building.bedrooms if building else None
            bathrooms = building.bathrooms if building else None
            quality_code = building.quality_code if building else None

            # Get extra features (pool, garage, etc.)
            features = list(prop.extra_features.filter(is_active=True))
            features_text = (
                format_feature_list(features, max_features=5) if features else None
            )

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

    filters_applied = any([first_name, last_name, address, street_name, zip_code])

    if not filters_applied:
        # Return empty CSV if no filters
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="property_search.csv"'
        writer = csv.writer(response)
        writer.writerow(["No search criteria provided"])
        return response

    params = {
        "first_name": first_name,
        "last_name": last_name,
        "address": address,
        "street_name": street_name,
        "zip_code": zip_code,
        "sort": sort,
        "dir": direction,
    }

    qs = build_property_search_queryset(params)

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

    for prop in qs:
        assessed = prop.assessed_value or prop.value
        bldg_area = prop.building_area or 0
        ppsf = None
        if assessed and bldg_area and bldg_area > 0:
            try:
                ppsf = float(assessed) / float(bldg_area)
            except Exception:
                ppsf = None

        # Get building details
        building = prop.buildings.filter(is_active=True).first()
        bedrooms = building.bedrooms if building else ""
        bathrooms = (
            f"{float(building.bathrooms):.1f}"
            if building and building.bathrooms
            else ""
        )
        quality_code = building.quality_code if building else ""

        # Get extra features
        features = list(prop.extra_features.filter(is_active=True))
        features_text = (
            format_feature_list(features, max_features=10) if features else ""
        )

        writer.writerow(
            [
                prop.account_number,
                prop.owner_name,
                prop.street_number,
                prop.street_name,
                prop.zipcode,
                assessed if assessed else "",
                bldg_area if bldg_area else "",
                bedrooms,
                bathrooms,
                quality_code,
                features_text,
                f"{ppsf:.2f}" if ppsf else "",
            ]
        )

    return response


def similar_properties(request, account_number):
    """Find and display properties similar to the given account."""
    # Use filter().first() to handle potential duplicates
    target_property = PropertyRecord.objects.filter(
        account_number=account_number
    ).first()

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

    # Get search parameters
    max_distance = float(request.GET.get("max_distance", "10"))
    max_results = int(request.GET.get("max_results", "20"))
    min_score = float(request.GET.get("min_score", "30"))

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
    target_bldg_area = target_building.heat_area if target_building else (target_property.building_area or 0)
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

    # Sort comparable properties by price per square foot (target always first)
    target_entry = next((r for r in formatted_results if r.get("is_target")), None)
    comparable_entries = [r for r in formatted_results if not r.get("is_target")]

    def ppsf_sort_key(entry):
        value = entry.get("ppsf")
        return float(value) if value is not None else float("inf")

    comparable_entries.sort(key=ppsf_sort_key)

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
                    comparable_ppsf_values_sorted[mid - 1]
                    + comparable_ppsf_values_sorted[mid]
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
            over_percentage = ((float(target_ppsf) - float(ppsf_median)) / float(ppsf_median)) * 100.0

            # Generate recommendation based on thresholds
            if over_percentage >= 20:
                protest_recommendation_level = "strong"
                protest_recommendation = "Recommend protesting"
                protest_recommendation_reason = (
                    f"Your price per sqft (${target_ppsf:.2f}) is about {over_percentage:.0f}% above "
                    f"the median (${ppsf_median:.2f}) of {comparable_count} similar properties "
                    f"(avg similarity score {comparable_avg_score:.0f}%)."
                )
            elif over_percentage >= 10:
                protest_recommendation_level = "moderate"
                protest_recommendation = "Consider protesting"
                protest_recommendation_reason = (
                    f"Your price per sqft (${target_ppsf:.2f}) is about {over_percentage:.0f}% above "
                    f"the median (${ppsf_median:.2f}) of {comparable_count} similar properties "
                    f"(avg similarity score {comparable_avg_score:.0f}%)."
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

    context = {
        "target_property": target_property,
        "target_building": target_building,
        "target_features": format_feature_list(target_features),
        "target_year_built": target_building.year_built if target_building else None,
        "target_bedrooms": target_building.bedrooms if target_building else None,
        "target_bathrooms": target_building.bathrooms if target_building else None,
        "target_quality_code": (
            target_building.quality_code if target_building else None
        ),
        "target_area": (
            target_building.heat_area
            if target_building
            else target_property.building_area
        ),
        "target_ppsf": target_ppsf,
        "target_ppsf_percentile": target_ppsf_percentile,
        "results": formatted_results,
        "results_sort_label": "price per square foot (lowest first)",
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
