"""
Similarity search algorithm for finding comparable properties.
Uses location (lat/long), size, age, and features to find similar properties.
"""

from math import asin, cos, radians, sin, sqrt
from typing import TYPE_CHECKING, Optional

from django.db.models import ExpressionWrapper, F, FloatField, Value
from django.db.models.functions import ACos, Cos, Greatest, Least, Radians, Sin

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


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _interpolate_curve(value: float, curve: list[tuple[float, float]]) -> float:
    """Return a smoothed similarity value from a piecewise linear curve."""
    if not curve:
        return 0.0

    if value <= curve[0][0]:
        return curve[0][1]

    for (start_x, start_y), (end_x, end_y) in zip(curve, curve[1:]):
        if value <= end_x:
            if end_x == start_x:
                return end_y

            ratio = (value - start_x) / (end_x - start_x)
            return start_y + ((end_y - start_y) * ratio)

    return curve[-1][1]


def _safe_float(value: object) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalized_code(value: object) -> str:
    return str(value or "").strip().upper()


def _percentage_similarity(
    target_value: object,
    candidate_value: object,
    curve: list[tuple[float, float]],
) -> float | None:
    target_num = _safe_float(target_value)
    candidate_num = _safe_float(candidate_value)

    if target_num is None or candidate_num is None or target_num <= 0:
        return None

    diff_pct = abs(target_num - candidate_num) / target_num
    return _clamp(_interpolate_curve(diff_pct, curve))


def _difference_similarity(
    target_value: object,
    candidate_value: object,
    curve: list[tuple[float, float]],
) -> float | None:
    target_num = _safe_float(target_value)
    candidate_num = _safe_float(candidate_value)

    if target_num is None or candidate_num is None:
        return None

    return _clamp(_interpolate_curve(abs(target_num - candidate_num), curve))


def _ranked_code_similarity(
    target_code: object,
    candidate_code: object,
    rank_map: dict[str, int],
) -> float | None:
    normalized_target = _normalized_code(target_code)
    normalized_candidate = _normalized_code(candidate_code)

    if not normalized_target or not normalized_candidate:
        return None

    if normalized_target == normalized_candidate:
        return 1.0

    target_rank = rank_map.get(normalized_target)
    candidate_rank = rank_map.get(normalized_candidate)

    if target_rank is None or candidate_rank is None:
        return None

    return _clamp(
        _interpolate_curve(
            abs(target_rank - candidate_rank),
            [(0.0, 1.0), (1.0, 0.72), (2.0, 0.42), (3.0, 0.18), (5.0, 0.0)],
        )
    )


def _categorical_similarity(target_code: object, candidate_code: object) -> float | None:
    normalized_target = _normalized_code(target_code)
    normalized_candidate = _normalized_code(candidate_code)

    if not normalized_target or not normalized_candidate:
        return None

    if normalized_target == normalized_candidate:
        return 1.0

    if len(normalized_target) >= 2 and len(normalized_candidate) >= 2:
        if normalized_target[:2] == normalized_candidate[:2]:
            return 0.65

    if normalized_target[0] == normalized_candidate[0]:
        return 0.4

    return 0.0


def _condition_similarity(target_code: object, candidate_code: object) -> float | None:
    ranked_similarity = _ranked_code_similarity(target_code, candidate_code, QUALITY_RANK)
    if ranked_similarity is not None:
        return ranked_similarity

    return _categorical_similarity(target_code, candidate_code)


def _building_character_similarity(
    target_building: "BuildingDetail",
    candidate_building: "BuildingDetail",
) -> float | None:
    for attr_name in ("building_style", "building_type", "building_class"):
        similarity = _categorical_similarity(
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


def _distance_similarity(distance: float, max_distance_miles: float) -> float | None:
    if max_distance_miles <= 0:
        return None

    ratio = _clamp(distance / max_distance_miles)
    return _clamp(
        _interpolate_curve(
            ratio,
            [(0.0, 1.0), (0.1, 0.93), (0.25, 0.78), (0.5, 0.52), (0.75, 0.24), (1.0, 0.05)],
        )
    )


def get_similarity_label(score: float) -> str:
    """Return a user-facing label for a 0-100 match score."""
    if score >= 84:
        return "Best match"
    if score >= 70:
        return "Highly similar"
    if score >= 52:
        return "Good match"
    if score >= 36:
        return "OK match"
    return "Broad match"


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great circle distance between two points on the earth in miles.

    Args:
        lat1, lon1: Latitude and longitude of first point
        lat2, lon2: Latitude and longitude of second point

    Returns:
        Distance in miles
    """
    # Convert decimal degrees to radians
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])

    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))

    # Radius of earth in miles
    miles = 3959 * c
    return miles


def calculate_similarity_score(
    target_prop: PropertyRecord,
    candidate_prop: PropertyRecord,
    target_building: BuildingDetail | None = None,
    candidate_building: BuildingDetail | None = None,
    target_features: list[ExtraFeature] | None = None,
    candidate_features: list[ExtraFeature] | None = None,
    distance: float = 0.0,
    max_distance_miles: float = 10.0,
) -> float:
    """
    Calculate a similarity score between two properties (0-100).

    Higher score = more similar

    Scoring weights (distance removed - used only for filtering):
    - Heated Size match: 22 points (±20% tolerance)
    - Lot Size match: 15 points (±20% tolerance)
    - Bedroom match: 18 points (exact match important)
    - Bathroom match: 18 points (exact match important)
    - Quality match: 12 points (same quality grade)
    - Feature match: 10 points (matching amenities)
    - Age match: 5 points (±5 years tolerance)

    Note: Distance is used to filter candidates (max_distance_miles parameter)
    but does not affect the similarity score. This allows comparison of
    properties with similar attributes regardless of distance.
    """
    components: list[tuple[str, float, float | None]] = []
    is_land_only = target_building is None and candidate_building is None

    if not is_land_only and target_building and candidate_building:
        components.extend(
            [
                (
                    "living_area",
                    RESIDENTIAL_WEIGHTS["living_area"],
                    _percentage_similarity(
                        target_building.heat_area,
                        candidate_building.heat_area,
                        [
                            (0.0, 1.0),
                            (0.05, 0.97),
                            (0.10, 0.90),
                            (0.20, 0.72),
                            (0.30, 0.52),
                            (0.40, 0.34),
                            (0.50, 0.18),
                            (0.75, 0.0),
                        ],
                    ),
                ),
                (
                    "bedrooms",
                    RESIDENTIAL_WEIGHTS["bedrooms"],
                    _difference_similarity(
                        target_building.bedrooms,
                        candidate_building.bedrooms,
                        [(0.0, 1.0), (1.0, 0.68), (2.0, 0.35), (3.0, 0.12), (4.0, 0.0)],
                    ),
                ),
                (
                    "bathrooms",
                    RESIDENTIAL_WEIGHTS["bathrooms"],
                    _difference_similarity(
                        target_building.bathrooms,
                        candidate_building.bathrooms,
                        [(0.0, 1.0), (0.5, 0.82), (1.0, 0.54), (1.5, 0.26), (2.5, 0.0)],
                    ),
                ),
                (
                    "quality",
                    RESIDENTIAL_WEIGHTS["quality"],
                    _ranked_code_similarity(
                        target_building.quality_code,
                        candidate_building.quality_code,
                        QUALITY_RANK,
                    ),
                ),
                (
                    "condition",
                    RESIDENTIAL_WEIGHTS["condition"],
                    _condition_similarity(
                        target_building.condition_code,
                        candidate_building.condition_code,
                    ),
                ),
                (
                    "age",
                    RESIDENTIAL_WEIGHTS["age"],
                    _difference_similarity(
                        _effective_year(target_building),
                        _effective_year(candidate_building),
                        [
                            (0.0, 1.0),
                            (2.0, 0.95),
                            (5.0, 0.82),
                            (10.0, 0.60),
                            (15.0, 0.38),
                            (25.0, 0.12),
                            (40.0, 0.0),
                        ],
                    ),
                ),
                (
                    "stories",
                    RESIDENTIAL_WEIGHTS["stories"],
                    _difference_similarity(
                        target_building.stories,
                        candidate_building.stories,
                        [(0.0, 1.0), (0.5, 0.70), (1.0, 0.35), (2.0, 0.0)],
                    ),
                ),
                (
                    "building_character",
                    RESIDENTIAL_WEIGHTS["building_character"],
                    _building_character_similarity(target_building, candidate_building),
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
            (
                "land_size",
                land_weight,
                _percentage_similarity(
                    target_prop.land_area,
                    candidate_prop.land_area,
                    [
                        (0.0, 1.0),
                        (0.05, 0.95),
                        (0.10, 0.87),
                        (0.20, 0.70),
                        (0.35, 0.42),
                        (0.50, 0.24),
                        (0.80, 0.0),
                    ],
                ),
            ),
            ("features", feature_weight, _feature_similarity(target_features, candidate_features)),
            (
                "distance",
                distance_weight,
                _distance_similarity(distance, max_distance_miles),
            ),
        ]
    )

    total_possible_weight = sum(weight for _, weight, _ in components)
    available_components = [
        (weight, similarity) for _, weight, similarity in components if similarity is not None
    ]

    if not available_components or total_possible_weight <= 0:
        return 0.0

    available_weight = sum(weight for weight, _ in available_components)
    weighted_sum = sum(weight * similarity for weight, similarity in available_components)

    base_score = weighted_sum / available_weight
    coverage_ratio = 1.0 if is_land_only else (available_weight / total_possible_weight)
    completeness_multiplier = 1.0 if is_land_only else (0.8 + (0.2 * coverage_ratio))
    final_score = base_score * completeness_multiplier * 100.0

    return round(_clamp(final_score, lower=0.0, upper=100.0), 1)


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
        target = PropertyRecord.objects.filter(account_number=account_number).first()
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

    # Calculate bounding box for initial index-based filtering
    # 1 degree lat =~ 69 miles
    lat_range = max_distance_miles / 69.0
    lon_range = max_distance_miles / (69.0 * cos(radians(target_lat)))

    min_lat = target_lat - lat_range
    max_lat = target_lat + lat_range
    min_lon = target_lon - lon_range
    max_lon = target_lon + lon_range

    # Query for nearby properties using Django ORM with annotations
    # This avoids raw SQL issues and "GROUP BY" errors while still being efficient

    # 1. Base filter by bounding box (uses database index)
    candidates = PropertyRecord.objects.filter(
        latitude__gte=min_lat,
        latitude__lte=max_lat,
        longitude__gte=min_lon,
        longitude__lte=max_lon,
        latitude__isnull=False,
        longitude__isnull=False,
    ).exclude(account_number=account_number)

    # 2. Optional: Filter by size if we have target building data
    if target_building and target_building.heat_area:
        min_area = float(target_building.heat_area) * 0.5
        max_area = float(target_building.heat_area) * 1.5

        # Use subquery to filter efficiently
        matching_buildings = BuildingDetail.objects.filter(
            is_active=True, heat_area__gte=min_area, heat_area__lte=max_area
        ).values("account_number")

        candidates = candidates.filter(account_number__in=matching_buildings)

    # 3. Annotate with distance calculation and filter
    # Formula: 3959 * acos(cos(radians(lat1)) * cos(radians(lat2)) * cos(radians(long2) - radians(long1)) + sin(radians(lat1)) * sin(radians(lat2)))

    # We use Value() for constants (target lat/lon) and F() for DB fields
    # Ensure float conversion for constants to avoid type issues
    target_lat_rad = radians(target_lat)
    target_lon_rad = radians(target_lon)

    candidates = (
        candidates.annotate(
            distance=ExpressionWrapper(
                3959.0
                * ACos(
                    Least(
                        1.0,
                        Greatest(
                            -1.0,
                            Cos(Value(target_lat_rad))
                            * Cos(Radians(F("latitude")))
                            * Cos(Radians(F("longitude")) - Value(target_lon_rad))
                            + Sin(Value(target_lat_rad)) * Sin(Radians(F("latitude"))),
                        ),
                    )
                ),
                output_field=FloatField(),
            )
        )
        .filter(distance__lte=max_distance_miles)
        .order_by("distance")
    )

    # Limit the number of candidates we process in Python
    # We fetch more than max_results to allow for filtering by similarity score
    candidates = candidates[:2000]

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

        # Calculate score
        score = calculate_similarity_score(
            target,
            candidate,
            target_building,
            c_building,
            target_features,
            c_features,
            dist,
            max_distance_miles=max_distance_miles,
        )

        if score >= min_score:
            results.append(
                {
                    "property": candidate,
                    "building": c_building,
                    "features": c_features,
                    "distance": round(dist, 2),
                    "similarity_score": score,
                }
            )

    # Sort and limit
    results.sort(
        key=lambda x: (
            -x["similarity_score"],
            x["distance"],
            x["property"].account_number,
        )
    )
    return results[:max_results]


def get_feature_summary(features: list[ExtraFeature]) -> dict[str, int]:
    """
    Get a summary of features by category.

    Returns:
        Dictionary with feature counts: {'POOL': 1, 'DETGAR': 2, ...}
    """
    summary = {}
    for feature in features:
        code = feature.feature_code
        if code:
            summary[code] = summary.get(code, 0) + 1
    return summary


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
