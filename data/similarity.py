"""
Similarity search algorithm for finding comparable properties.
Uses location (lat/long), size, age, and features to find similar properties.
"""

from math import radians, cos, sin, asin, sqrt
from typing import List, Dict, Optional, TYPE_CHECKING
from .models import PropertyRecord, BuildingDetail, ExtraFeature

if TYPE_CHECKING:
    from decimal import Decimal


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
    target_building: Optional[BuildingDetail] = None,
    candidate_building: Optional[BuildingDetail] = None,
    target_features: Optional[List[ExtraFeature]] = None,
    candidate_features: Optional[List[ExtraFeature]] = None,
    distance: float = 0.0,
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
    score = 0.0

    # Distance is NOT scored - it only filters candidates
    # This allows finding similar properties for price/sqft comparison
    # regardless of whether they're 1 mile or 10 miles away

    if target_building and candidate_building:
        # Heated size matching (22 points max)
        if target_building.heat_area and candidate_building.heat_area:
            target_area = float(target_building.heat_area)
            candidate_area = float(candidate_building.heat_area)

            # Calculate percentage difference
            diff_pct = abs(target_area - candidate_area) / target_area

            if diff_pct <= 0.1:  # Within 10%
                score += 22
            elif diff_pct <= 0.2:  # Within 20%
                score += 18
            elif diff_pct <= 0.3:  # Within 30%
                score += 11
            elif diff_pct <= 0.5:  # Within 50%
                score += 6

        # Age matching (5 points max)
        if target_building.year_built and candidate_building.year_built:
            year_diff = abs(target_building.year_built - candidate_building.year_built)

            if year_diff <= 2:
                score += 5
            elif year_diff <= 5:
                score += 4
            elif year_diff <= 10:
                score += 3
            elif year_diff <= 15:
                score += 2

        # Quality matching (12 points max)
        # X=Superior, A=Excellent, B=Good, C=Average, D=Low, E=Very Low, F=Poor
        if target_building.quality_code and candidate_building.quality_code:
            target_q = target_building.quality_code.strip().upper()
            candidate_q = candidate_building.quality_code.strip().upper()

            if target_q == candidate_q:
                score += 12  # Exact quality match
            else:
                # Define quality ranking (higher = better)
                quality_rank = {"X": 7, "A": 6, "B": 5, "C": 4, "D": 3, "E": 2, "F": 1}
                target_rank = quality_rank.get(target_q, 0)
                candidate_rank = quality_rank.get(candidate_q, 0)

                if target_rank > 0 and candidate_rank > 0:
                    rank_diff = abs(target_rank - candidate_rank)
                    if rank_diff == 1:
                        score += 8  # One quality level off
                    elif rank_diff == 2:
                        score += 4  # Two quality levels off

        # Bedroom matching (18 points max)
        if target_building.bedrooms and candidate_building.bedrooms:
            if target_building.bedrooms == candidate_building.bedrooms:
                score += 18  # Exact match
            elif abs(target_building.bedrooms - candidate_building.bedrooms) == 1:
                score += 10  # Off by 1
            elif abs(target_building.bedrooms - candidate_building.bedrooms) == 2:
                score += 5  # Off by 2

        # Bathroom matching (18 points max)
        if target_building.bathrooms and candidate_building.bathrooms:
            bath_diff = abs(
                float(target_building.bathrooms) - float(candidate_building.bathrooms)
            )
            if bath_diff <= 0.5:
                score += 18  # Exact or half-bath difference
            elif bath_diff <= 1.0:
                score += 11  # One full bath difference
            elif bath_diff <= 1.5:
                score += 5  # 1.5 bath difference

    # Feature matching (10 points max)
    if target_features and candidate_features:
        target_codes = set(f.feature_code for f in target_features)
        candidate_codes = set(f.feature_code for f in candidate_features)

        if target_codes and candidate_codes:
            # Calculate Jaccard similarity for features
            intersection = len(target_codes & candidate_codes)
            union = len(target_codes | candidate_codes)

            if union > 0:
                feature_similarity = intersection / union
                score += feature_similarity * 10
    
    # Lot size matching (15 points max)
    # Uses PropertyRecord.land_area (total land square footage)
    if target_prop.land_area and candidate_prop.land_area:
        try:
            target_land = float(target_prop.land_area)
            candidate_land = float(candidate_prop.land_area)
            if target_land > 0:
                land_diff_pct = abs(target_land - candidate_land) / target_land
                if land_diff_pct <= 0.1:  # Within 10%
                    score += 15
                elif land_diff_pct <= 0.2:  # Within 20%
                    score += 12
                elif land_diff_pct <= 0.3:  # Within 30%
                    score += 8
                elif land_diff_pct <= 0.5:  # Within 50%
                    score += 4
        except (ValueError, TypeError):
            pass

    return round(score, 1)


def find_similar_properties(
    account_number: str,
    max_distance_miles: float = 10.0,
    max_results: int = 50,
    min_score: float = 30.0,
) -> List[Dict]:
    """
    Find properties similar to the given account number.

    Args:
        account_number: The account number to find matches for
            max_distance_miles: Maximum distance in miles (default: 10)
        max_results: Maximum number of results to return (default: 50)
        min_score: Minimum similarity score (0-100) to include (default: 30)

    Returns:
        List of dictionaries with property details and similarity info:
        {
            'property': PropertyRecord,
            'building': BuildingDetail,
            'features': List[ExtraFeature],
            'distance': float (miles),
            'similarity_score': float (0-100),
        }
    """
    # Get the target property (use first() to handle potential duplicates)
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

    # Get target building and features (only active records)
    target_building = target.buildings.filter(is_active=True).first()  # type: ignore[attr-defined]
    target_features = list(target.extra_features.filter(is_active=True))  # type: ignore[attr-defined]

    # Calculate rough bounding box for filtering
    # 1 degree of latitude ≈ 69 miles
    # 1 degree of longitude ≈ 69 * cos(latitude) miles
    lat_range = max_distance_miles / 69.0
    lon_range = max_distance_miles / (69.0 * cos(radians(target_lat)))

    # Query for nearby properties with coordinates
    candidates = (
        PropertyRecord.objects.filter(
            latitude__gte=target_lat - lat_range,
            latitude__lte=target_lat + lat_range,
            longitude__gte=target_lon - lon_range,
            longitude__lte=target_lon + lon_range,
            latitude__isnull=False,
            longitude__isnull=False,
        )
        .exclude(account_number=account_number)  # Exclude the target property itself
        .distinct()  # Ensure unique properties
        .select_related()
        .prefetch_related("buildings", "extra_features")
    )

    # Optional: Filter by size if we have target building data
    if target_building and target_building.heat_area:
        min_area = float(target_building.heat_area) * 0.5  # 50% smaller
        max_area = float(target_building.heat_area) * 1.5  # 50% larger
        # Use a subquery to avoid duplicates from multiple buildings
        candidates = candidates.filter(
            account_number__in=BuildingDetail.objects.filter(
                is_active=True,
                heat_area__gte=min_area,
                heat_area__lte=max_area,
            )
            .values_list("account_number", flat=True)
            .distinct()
        )

    # Calculate distances and similarity scores
    # Process candidates in batches for performance while ensuring quality results
    results = []
    processed = 0
    batch_size = 1000
    
    # Process candidates in batches until we have enough good matches
    # or we've exhausted all candidates
    for offset in range(0, candidates.count(), batch_size):
        batch = candidates[offset:offset + batch_size]
        
        for candidate in batch:
            # Skip if coordinates are missing
            if not candidate.latitude or not candidate.longitude:
                continue
                
            candidate_lat = float(candidate.latitude)
            candidate_lon = float(candidate.longitude)

            # Calculate distance
            distance = haversine_distance(
                target_lat, target_lon, candidate_lat, candidate_lon
            )

            # Skip if too far
            if distance > max_distance_miles:
                continue
            
            processed += 1

        # Get candidate building and features (only active records)
        candidate_building = candidate.buildings.filter(is_active=True).first()  # type: ignore[attr-defined]
        candidate_features = list(candidate.extra_features.filter(is_active=True))  # type: ignore[attr-defined]

        # Calculate similarity score
        score = calculate_similarity_score(
            target,
            candidate,
            target_building,
            candidate_building,
            target_features,
            candidate_features,
            distance,
        )

        # Skip if score is too low
        if score < min_score:
            continue

        results.append(
            {
                "property": candidate,
                "building": candidate_building,
                "features": candidate_features,
                "distance": round(distance, 2),
                "similarity_score": score,
            }
        )
        
        # Early termination: if we have 3x max_results with good scores, stop processing
        # This balances thoroughness with performance
        if len(results) >= max_results * 3:
            # Check if we have enough high-quality results
            high_quality_count = sum(1 for r in results if r["similarity_score"] >= 50)
            if high_quality_count >= max_results:
                break
        
        # Absolute limit to prevent runaway processing
        if processed >= 50000:
            break
    
    # Sort by similarity score (highest first)
    results.sort(key=lambda x: x["similarity_score"], reverse=True)

    # Return top N results
    return results[:max_results]


def get_feature_summary(features: List[ExtraFeature]) -> Dict[str, int]:
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


def format_feature_list(features: List[ExtraFeature], max_features: int = 10) -> str:
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
