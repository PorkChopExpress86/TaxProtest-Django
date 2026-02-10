"""
Similarity search algorithm for finding comparable properties.
Uses location (lat/long), size, age, and features to find similar properties.
"""

from math import radians, cos, sin, asin, sqrt
from typing import List, Dict, Optional, TYPE_CHECKING
from django.db.models import F, Value, FloatField, ExpressionWrapper
from django.db.models.functions import ACos, Cos, Radians, Sin, Least, Greatest
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

    # Determine if we are comparing land-only (no target building)
    is_land_only = target_building is None

    if not is_land_only and target_building and candidate_building:
        # Standard Scoring (Building + Land)
        
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
    
    # Lot size matching
    # Standard: 15 points max
    # Land Only: 90 points max (since filtering out building criteria)
    
    max_land_points = 90.0 if is_land_only else 15.0
    
    # Uses PropertyRecord.land_area (total land square footage)
    if target_prop.land_area and candidate_prop.land_area:
        try:
            target_land = float(target_prop.land_area)
            candidate_land = float(candidate_prop.land_area)
            if target_land > 0:
                land_diff_pct = abs(target_land - candidate_land) / target_land
                
                points = 0
                if is_land_only:
                    # Granular scoring for land-only to avoid "all 90%"
                    if land_diff_pct <= 0.005: # 0.5% or less
                        points = max_land_points 
                    elif land_diff_pct <= 0.05: # 0.5-5%
                        # Linear drop from 90 to 80
                        ratio = (land_diff_pct - 0.005) / 0.045
                        points = 90 - (10 * ratio)
                    elif land_diff_pct <= 0.1: # 5-10%
                        # Linear drop from 80 to 70
                        ratio = (land_diff_pct - 0.05) / 0.05
                        points = 80 - (10 * ratio)
                    elif land_diff_pct <= 0.2: # 10-20%
                        # Linear drop from 70 to 50
                        ratio = (land_diff_pct - 0.1) / 0.1
                        points = 70 - (20 * ratio)
                    elif land_diff_pct <= 0.5: # 20-50%
                        # Linear drop from 50 to 20
                        ratio = (land_diff_pct - 0.2) / 0.3
                        points = 50 - (30 * ratio)
                    elif land_diff_pct <= 1.0: # 50-100%
                        points = 15.0
                else: 
                     # Standard building + land scoring
                    if land_diff_pct <= 0.1:  # Within 10%
                        points = max_land_points
                    elif land_diff_pct <= 0.2:  # Within 20%
                        points = max_land_points * 0.8
                    elif land_diff_pct <= 0.3:
                         points = max_land_points * 0.53
                    elif land_diff_pct <= 0.5:
                         points = max_land_points * 0.27
                
                score += points
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
            is_active=True,
            heat_area__gte=min_area,
            heat_area__lte=max_area
        ).values('account_number')
        
        candidates = candidates.filter(account_number__in=matching_buildings)

    # 3. Annotate with distance calculation and filter
    # Formula: 3959 * acos(cos(radians(lat1)) * cos(radians(lat2)) * cos(radians(long2) - radians(long1)) + sin(radians(lat1)) * sin(radians(lat2)))
    
    # We use Value() for constants (target lat/lon) and F() for DB fields
    # Ensure float conversion for constants to avoid type issues
    target_lat_rad = radians(target_lat)
    target_lon_rad = radians(target_lon)
    
    candidates = candidates.annotate(
        distance=ExpressionWrapper(
            3959.0 * ACos(
                Least(1.0, Greatest(-1.0,
                    Cos(Value(target_lat_rad)) * Cos(Radians(F('latitude'))) *
                    Cos(Radians(F('longitude')) - Value(target_lon_rad)) +
                    Sin(Value(target_lat_rad)) * Sin(Radians(F('latitude')))
                ))
            ),
            output_field=FloatField()
        )
    ).filter(
        distance__lte=max_distance_miles
    ).order_by('distance')
    
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
        dist = getattr(candidate, 'distance', 0.0)
        
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
        )
        
        if score >= min_score:
            results.append({
                "property": candidate,
                "building": c_building,
                "features": c_features,
                "distance": round(dist, 2),
                "similarity_score": score,
            })
            
    # Sort and limit
    results.sort(key=lambda x: x["similarity_score"], reverse=True)
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
