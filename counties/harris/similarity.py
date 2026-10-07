"""
Similarity search algorithm for finding comparable properties.
Uses location (lat/long), size, age, and features to find similar properties.
"""

from typing import TYPE_CHECKING, Optional

from counties.common.similarity_math import (
    AGE_CURVE,
    BATHROOMS_CURVE,
    BEDROOMS_CURVE,
    LAND_SIZE_CURVE,
    LIVING_AREA_CURVE,
    STORIES_CURVE,
    categorical_similarity,
    component,
    difference_similarity,
    distance_similarity,
    nearby_properties,
    percentage_similarity,
    ranked_code_similarity,
    score_from_components,
)

from .models import BuildingDetail, ExtraFeature, PropertyRecord

if TYPE_CHECKING:
    pass


QUALITY_RANK = {"X": 7, "A": 6, "B": 5, "C": 4, "D": 3, "E": 2, "F": 1}

RESIDENTIAL_WEIGHTS = {
    "living_area": 24.0,
    "land_size": 10.0,
    "bedrooms": 14.0,
    "bathrooms": 12.0,
    "quality": 10.0,
    "condition": 6.0,
    "age": 8.0,
    "stories": 4.0,
    "building_character": 4.0,
    "features": 4.0,
    "distance": 4.0,
}

LAND_ONLY_WEIGHTS = {
    "land_size": 80.0,
    "features": 10.0,
    "distance": 10.0,
}

COMPONENT_LABELS = {
    "living_area": "Living Area",
    "land_size": "Land Size",
    "bedrooms": "Bedrooms",
    "bathrooms": "Bathrooms",
    "quality": "Quality",
    "condition": "Condition",
    "age": "Age",
    "stories": "Stories",
    "building_character": "Building Type",
    "features": "Features",
    "distance": "Distance",
}


def _condition_similarity(target_code: object, candidate_code: object) -> float | None:
    ranked_similarity = ranked_code_similarity(target_code, candidate_code, QUALITY_RANK)
    if ranked_similarity is not None:
        return ranked_similarity

    return categorical_similarity(target_code, candidate_code)


def _building_character_similarity(
    target_building: "BuildingDetail",
    candidate_building: "BuildingDetail",
) -> float | None:
    for attr_name in ("building_style", "building_type", "building_class"):
        similarity = categorical_similarity(
            getattr(target_building, attr_name, None),
            getattr(candidate_building, attr_name, None),
        )
        if similarity is not None:
            return similarity

    return None


def _effective_year(building: Optional["BuildingDetail"]) -> int | None:
    if building is None:
        return None

    for attr_name in ("effective_year", "year_remodeled", "year_built"):
        value = getattr(building, attr_name, None)
        if value:
            return int(value)

    return None


def _feature_similarity(
    target_features: list[ExtraFeature] | None,
    candidate_features: list[ExtraFeature] | None,
) -> float | None:
    if target_features is None or candidate_features is None:
        return None

    target_codes = {f.feature_code for f in target_features if f.feature_code}
    candidate_codes = {f.feature_code for f in candidate_features if f.feature_code}

    if not target_codes and not candidate_codes:
        return None

    union = len(target_codes | candidate_codes)
    if union == 0:
        return None

    intersection = len(target_codes & candidate_codes)
    return intersection / union


def calculate_similarity_details(
    target_prop: PropertyRecord,
    candidate_prop: PropertyRecord,
    target_building: BuildingDetail | None = None,
    candidate_building: BuildingDetail | None = None,
    target_features: list[ExtraFeature] | None = None,
    candidate_features: list[ExtraFeature] | None = None,
    distance: float = 0.0,
    max_distance_miles: float = 10.0,
) -> dict[str, object]:
    """Calculate an explainable similarity score and component breakdown."""
    components: list[dict[str, object]] = []
    is_land_only = target_building is None and candidate_building is None

    if not is_land_only and target_building and candidate_building:
        components.extend(
            [
                component(
                    "living_area",
                    RESIDENTIAL_WEIGHTS["living_area"],
                    percentage_similarity(
                        target_building.heat_area,
                        candidate_building.heat_area,
                        LIVING_AREA_CURVE,
                    ),
                    labels=COMPONENT_LABELS,
                ),
                component(
                    "bedrooms",
                    RESIDENTIAL_WEIGHTS["bedrooms"],
                    difference_similarity(
                        target_building.bedrooms,
                        candidate_building.bedrooms,
                        BEDROOMS_CURVE,
                    ),
                    labels=COMPONENT_LABELS,
                ),
                component(
                    "bathrooms",
                    RESIDENTIAL_WEIGHTS["bathrooms"],
                    difference_similarity(
                        target_building.bathrooms,
                        candidate_building.bathrooms,
                        BATHROOMS_CURVE,
                    ),
                    labels=COMPONENT_LABELS,
                ),
                component(
                    "quality",
                    RESIDENTIAL_WEIGHTS["quality"],
                    ranked_code_similarity(
                        target_building.quality_code,
                        candidate_building.quality_code,
                        QUALITY_RANK,
                    ),
                    labels=COMPONENT_LABELS,
                ),
                component(
                    "condition",
                    RESIDENTIAL_WEIGHTS["condition"],
                    _condition_similarity(
                        target_building.condition_code,
                        candidate_building.condition_code,
                    ),
                    labels=COMPONENT_LABELS,
                ),
                component(
                    "age",
                    RESIDENTIAL_WEIGHTS["age"],
                    difference_similarity(
                        _effective_year(target_building),
                        _effective_year(candidate_building),
                        AGE_CURVE,
                    ),
                    labels=COMPONENT_LABELS,
                ),
                component(
                    "stories",
                    RESIDENTIAL_WEIGHTS["stories"],
                    difference_similarity(
                        target_building.stories,
                        candidate_building.stories,
                        STORIES_CURVE,
                    ),
                    labels=COMPONENT_LABELS,
                ),
                component(
                    "building_character",
                    RESIDENTIAL_WEIGHTS["building_character"],
                    _building_character_similarity(target_building, candidate_building),
                    labels=COMPONENT_LABELS,
                ),
            ]
        )

    land_weight = (
        LAND_ONLY_WEIGHTS["land_size"] if is_land_only else RESIDENTIAL_WEIGHTS["land_size"]
    )
    feature_weight = (
        LAND_ONLY_WEIGHTS["features"] if is_land_only else RESIDENTIAL_WEIGHTS["features"]
    )
    distance_weight = (
        LAND_ONLY_WEIGHTS["distance"] if is_land_only else RESIDENTIAL_WEIGHTS["distance"]
    )

    components.extend(
        [
            component(
                "land_size",
                land_weight,
                percentage_similarity(
                    target_prop.land_area,
                    candidate_prop.land_area,
                    LAND_SIZE_CURVE,
                ),
                labels=COMPONENT_LABELS,
            ),
            component(
                "features",
                feature_weight,
                _feature_similarity(target_features, candidate_features),
                labels=COMPONENT_LABELS,
            ),
            component(
                "distance",
                distance_weight,
                distance_similarity(distance, max_distance_miles),
                labels=COMPONENT_LABELS,
            ),
        ]
    )

    return score_from_components(components, is_land_only=is_land_only)


def find_similar_properties(
    account_number: str,
    max_distance_miles: float = 10.0,
    max_results: int = 50,
    min_score: float = 30.0,
) -> list[dict]:
    """
    Find properties similar to the given account number.
    Optimized to perform distance calculation in the database.
    """
    # Get the target property
    try:
        target = PropertyRecord.objects.filter(
            account_number=account_number, is_residential=True, is_data_ready=True
        ).first()
        if not target:
            return []
    except Exception:
        return []

    # Check if target has coordinates
    if not target.latitude or not target.longitude:
        return []

    target_lat = float(target.latitude)
    target_lon = float(target.longitude)

    # Get target building and features
    target_building = target.buildings.filter(is_active=True).first()  # type: ignore[attr-defined]
    target_features = list(target.extra_features.filter(is_active=True))  # type: ignore[attr-defined]

    # Harris pre-filters; the shared query adds the radius, order and cap.
    candidates = PropertyRecord.objects.filter(
        is_residential=True,
        is_data_ready=True,
    ).exclude(account_number=account_number)

    # Optional: filter by size if we have target building data
    if target_building and target_building.heat_area:
        min_area = float(target_building.heat_area) * 0.5
        max_area = float(target_building.heat_area) * 1.5

        # Use subquery to filter efficiently
        matching_buildings = BuildingDetail.objects.filter(
            is_active=True, heat_area__gte=min_area, heat_area__lte=max_area
        ).values("account_number")

        candidates = candidates.filter(account_number__in=matching_buildings)

    candidates = nearby_properties(
        candidates,
        latitude=target_lat,
        longitude=target_lon,
        max_distance_miles=max_distance_miles,
    )

    # Process candidates
    results = []

    # Fetch related data efficiently
    # Since we sliced the queryset, we need to evaluate it to get the list of objects
    # and then fetch related data for those specific objects
    candidate_list = list(candidates)

    if not candidate_list:
        return []

    candidate_accts = [c.account_number for c in candidate_list]

    # Bulk fetch buildings
    buildings_map = {}
    for b in BuildingDetail.objects.filter(account_number__in=candidate_accts, is_active=True):
        buildings_map[b.account_number] = b

    # Bulk fetch features
    from collections import defaultdict

    features_map = defaultdict(list)
    for f in ExtraFeature.objects.filter(account_number__in=candidate_accts, is_active=True):
        features_map[f.account_number].append(f)

    # Calculate scores
    for candidate in candidate_list:
        dist = getattr(candidate, "distance", 0.0)

        c_building = buildings_map.get(candidate.account_number)
        c_features = features_map.get(candidate.account_number, [])

        details = calculate_similarity_details(
            target,
            candidate,
            target_building,
            c_building,
            target_features,
            c_features,
            dist,
            max_distance_miles=max_distance_miles,
        )
        score = float(details["score"])

        if score >= min_score:
            results.append(
                {
                    "property": candidate,
                    "building": c_building,
                    "features": c_features,
                    "distance": round(dist, 2),
                    "similarity_score": score,
                    "score_breakdown": details["components"],
                }
            )

    # Rank by score (then distance) before truncating to max_results, so the
    # candidates that get cut are the weakest matches, not just the farthest
    # ones the bounding-box query happened to enumerate first. This ordering
    # is for *selecting* the top N, not for how they're displayed — the
    # shared web layer's ``sort_comps_for_display`` owns final page order.
    results.sort(
        key=lambda x: (
            -x["similarity_score"],
            x["distance"],
            x["property"].account_number,
        )
    )
    return results[:max_results]


def format_feature_list(features: list[ExtraFeature], max_features: int = 10) -> str:
    """
    Format a list of features into a readable string using feature descriptions.

    Returns:
        Comma-separated list like "Reinforced Concrete Pool, Frame Detached Garage"
    """
    # Group features by description and count them
    feature_counts = {}
    for feature in features:
        desc = feature.feature_description or feature.feature_code or "Unknown"
        feature_counts[desc] = feature_counts.get(desc, 0) + 1

    # Format as readable list
    items = []
    for desc, count in sorted(feature_counts.items())[:max_features]:
        if count > 1:
            items.append(f"{desc} ({count})")
        else:
            items.append(desc)

    return ", ".join(items) if items else "None"
