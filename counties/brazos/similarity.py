"""Similarity search for comparable Brazos properties.

Deliberately NOT a generalization of data/similarity.py (wayfinder ticket
#9): that module's model-coupled functions (find_similar_properties,
calculate_similarity_details, _building_character_similarity,
_feature_similarity) take PropertyRecord/BuildingDetail/ExtraFeature
instances directly with no dict/protocol abstraction layer, so adapting them
would be a real rewrite, not a parameterization. The small handful of pure
math/curve helpers below (_clamp, _interpolate_curve, _percentage_similarity,
etc.) have zero model coupling and are intentionally duplicated rather than
imported, so this module stays self-contained -- see data/similarity.py if
those two copies ever need to double-check each other.

Weighting differs from Harris's RESIDENTIAL_WEIGHTS in one structural way:
Brazos has no confirmed whole-building *condition* rating separate from
*quality* (wayfinder ticket #12) -- the GIS shapefile's class_code digit is
a verified quality-tier signal (monotonic with $/sqft) but doesn't split
into two independent axes the way Harris's quality_code/condition_code do.
Their weights (10% + 6%) are combined into one 16% "quality" factor here
rather than faking a second, perfectly-correlated component.

Also per ticket #12: "stories" has no numeric field, but the real export's
detail_description carries a literal "SECOND FLOOR" value (no "THIRD FLOOR"
or higher exists) -- used here as a binary 1-vs-2 proxy, same 4% weight
Harris gives its own (numeric, finer-grained) stories field.

A property can have multiple improvements (20.4% of real properties do,
e.g. a detached garage alongside the main house). improvement_type='R'
("RESIDENTIAL", confirmed via real data: 74,059/94,291 improvement rows)
selects the main structure(s); among ties, the first (by imp_id) that has a
PropertyBuildingCharacteristic row wins -- a simple deterministic pick for
an edge case, not a semantic ranking.
"""

from __future__ import annotations

import re
from collections import defaultdict
from math import asin, cos, radians, sin, sqrt

from django.db.models import ExpressionWrapper, F, FloatField, Value
from django.db.models.functions import ACos, Cos, Greatest, Least, Radians, Sin

from .models import (
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyLand,
)

# How many nearest parcels find_similar_properties() pulls out of the database
# to score in Python. Every one of these slots must buy a scoreable comp, which
# is why the PropertyAccount filter is applied before the cap, not after.
MAX_GEOMETRY_CANDIDATES = 2000

RESIDENTIAL_WEIGHTS = {
    "living_area": 24.0,
    "land_size": 10.0,
    "bedrooms": 14.0,
    "bathrooms": 12.0,
    "quality": 16.0,  # combined quality+condition -- see module docstring
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
    "age": "Age",
    "stories": "Stories",
    "building_character": "Building Character",
    "features": "Features",
    "distance": "Distance",
}

CLASS_CODE_DIGIT_RE = re.compile(r"(\d)")


# ---------------------------------------------------------------- pure math helpers


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _interpolate_curve(value: float, curve: list[tuple[float, float]]) -> float:
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
    target_value: object, candidate_value: object, curve: list[tuple[float, float]]
) -> float | None:
    target_num = _safe_float(target_value)
    candidate_num = _safe_float(candidate_value)
    if target_num is None or candidate_num is None or target_num <= 0:
        return None
    diff_pct = abs(target_num - candidate_num) / target_num
    return _clamp(_interpolate_curve(diff_pct, curve))


def _difference_similarity(
    target_value: object, candidate_value: object, curve: list[tuple[float, float]]
) -> float | None:
    target_num = _safe_float(target_value)
    candidate_num = _safe_float(candidate_value)
    if target_num is None or candidate_num is None:
        return None
    return _clamp(_interpolate_curve(abs(target_num - candidate_num), curve))


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


def _distance_similarity(distance: float, max_distance_miles: float) -> float | None:
    if max_distance_miles <= 0:
        return None
    ratio = _clamp(distance / max_distance_miles)
    return _clamp(
        _interpolate_curve(
            ratio, [(0.0, 1.0), (0.1, 0.93), (0.25, 0.78), (0.5, 0.52), (0.75, 0.24), (1.0, 0.05)]
        )
    )


def get_similarity_label(score: float) -> str:
    """Return a user-facing label for a 0-100 match score. Same bands as
    Harris's (see CLAUDE.md's Similarity Algorithm section) -- the labels
    describe the score itself, not anything Harris-specific."""
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
    """Great-circle distance between two points in miles."""
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    return 3959 * c


def _component(name: str, weight: float, similarity: float | None) -> dict[str, object]:
    return {
        "name": name,
        "label": COMPONENT_LABELS.get(name, name.replace("_", " ").title()),
        "weight": weight,
        "similarity": None if similarity is None else round(similarity, 3),
        "points": None if similarity is None else round(weight * similarity, 1),
        "available": similarity is not None,
    }


def _score_from_components(
    components: list[dict[str, object]], *, is_land_only: bool
) -> dict[str, object]:
    total_possible_weight = sum(float(c["weight"]) for c in components)
    available_components = [
        (float(c["weight"]), float(c["similarity"]))
        for c in components
        if c["similarity"] is not None
    ]
    if not available_components or total_possible_weight <= 0:
        return {"score": 0.0, "components": components, "available_weight": 0.0}

    available_weight = sum(weight for weight, _ in available_components)
    weighted_sum = sum(weight * similarity for weight, similarity in available_components)
    base_score = weighted_sum / available_weight
    coverage_ratio = 1.0 if is_land_only else (available_weight / total_possible_weight)
    completeness_multiplier = 1.0 if is_land_only else (0.8 + (0.2 * coverage_ratio))
    final_score = base_score * completeness_multiplier * 100.0

    return {
        "score": round(_clamp(final_score, lower=0.0, upper=100.0), 1),
        "components": components,
        "available_weight": round(available_weight, 1),
    }


# ---------------------------------------------------------------- Brazos-specific helpers


def _quality_digit(class_code: object) -> int | None:
    match = CLASS_CODE_DIGIT_RE.search(_normalized_code(class_code))
    return int(match.group(1)) if match else None


def _quality_similarity(target_class_code: object, candidate_class_code: object) -> float | None:
    target_digit = _quality_digit(target_class_code)
    candidate_digit = _quality_digit(candidate_class_code)
    if target_digit is None or candidate_digit is None:
        return None
    return _clamp(
        _interpolate_curve(
            abs(target_digit - candidate_digit),
            [(0.0, 1.0), (1.0, 0.72), (2.0, 0.42), (3.0, 0.18), (5.0, 0.0)],
        )
    )


def _building_character_similarity(
    target_building: PropertyBuildingCharacteristic | None,
    candidate_building: PropertyBuildingCharacteristic | None,
) -> float | None:
    if target_building is None or candidate_building is None:
        return None
    for attr_name in ("exterior_wall", "construction_style", "foundation"):
        similarity = _categorical_similarity(
            getattr(target_building, attr_name, None), getattr(candidate_building, attr_name, None)
        )
        if similarity is not None:
            return similarity
    return None


def _feature_similarity(
    target_features: list[PropertyExtraFeature] | None,
    candidate_features: list[PropertyExtraFeature] | None,
) -> float | None:
    if target_features is None or candidate_features is None:
        return None
    target_types = {f.feature_type for f in target_features if f.feature_type}
    candidate_types = {f.feature_type for f in candidate_features if f.feature_type}
    if not target_types and not candidate_types:
        return None
    union = len(target_types | candidate_types)
    if union == 0:
        return None
    intersection = len(target_types & candidate_types)
    return intersection / union


def _primary_improvement(
    prop_id: str, tax_year: int
) -> tuple[PropertyImprovement | None, PropertyBuildingCharacteristic | None]:
    """Pick the residential improvement + its characteristics row to
    represent this property. See module docstring for why 'R'/first-match
    is the tiebreak for the 20.4% of properties with multiple improvements."""
    improvements = list(
        PropertyImprovement.objects.filter(
            prop_id=prop_id, tax_year=tax_year, improvement_type="R"
        ).order_by("imp_id")
    )
    if not improvements:
        return None, None

    characteristics_by_imp = {
        c.imp_id: c
        for c in PropertyBuildingCharacteristic.objects.filter(
            prop_id=prop_id, tax_year=tax_year, imp_id__in=[i.imp_id for i in improvements]
        )
    }
    for improvement in improvements:
        characteristic = characteristics_by_imp.get(improvement.imp_id)
        if characteristic is not None:
            return improvement, characteristic
    return improvements[0], None


def _has_second_floor(prop_id: str, imp_id: str, tax_year: int) -> bool:
    return PropertyImprovementDetail.objects.filter(
        prop_id=prop_id, imp_id=imp_id, tax_year=tax_year, detail_description="SECOND FLOOR"
    ).exists()


def _stories_value(prop_id: str, imp_id: str | None, tax_year: int) -> float | None:
    if imp_id is None:
        return None
    return 2.0 if _has_second_floor(prop_id, imp_id, tax_year) else 1.0


def _effective_year(
    improvement: PropertyImprovement | None, account: PropertyAccount
) -> int | None:
    if improvement is not None and improvement.year_built:
        return int(improvement.year_built)
    if account.year_built:
        return int(account.year_built)
    return None


def _total_acreage(prop_id: str, tax_year: int) -> float | None:
    rows = PropertyLand.objects.filter(prop_id=prop_id, tax_year=tax_year).values_list(
        "acreage", flat=True
    )
    values = [float(a) for a in rows if a is not None]
    return sum(values) if values else None


# ---------------------------------------------------------------- scoring


def calculate_similarity_details(
    target_account: PropertyAccount,
    candidate_account: PropertyAccount,
    target_improvement: PropertyImprovement | None = None,
    candidate_improvement: PropertyImprovement | None = None,
    target_building: PropertyBuildingCharacteristic | None = None,
    candidate_building: PropertyBuildingCharacteristic | None = None,
    target_features: list[PropertyExtraFeature] | None = None,
    candidate_features: list[PropertyExtraFeature] | None = None,
    target_acreage: float | None = None,
    candidate_acreage: float | None = None,
    distance: float = 0.0,
    max_distance_miles: float = 10.0,
) -> dict[str, object]:
    """Calculate an explainable similarity score and component breakdown."""
    components: list[dict[str, object]] = []
    is_land_only = target_building is None and candidate_building is None

    if not is_land_only:
        target_stories = _stories_value(
            target_account.prop_id,
            target_improvement.imp_id if target_improvement else None,
            target_account.tax_year,
        )
        candidate_stories = _stories_value(
            candidate_account.prop_id,
            candidate_improvement.imp_id if candidate_improvement else None,
            candidate_account.tax_year,
        )
        components.extend(
            [
                _component(
                    "living_area",
                    RESIDENTIAL_WEIGHTS["living_area"],
                    _percentage_similarity(
                        target_account.living_area,
                        candidate_account.living_area,
                        [
                            (0.0, 1.0),
                            (0.03, 0.96),
                            (0.05, 0.90),
                            (0.10, 0.78),
                            (0.20, 0.55),
                            (0.30, 0.32),
                            (0.40, 0.16),
                            (0.50, 0.06),
                            (0.75, 0.0),
                        ],
                    ),
                ),
                _component(
                    "bedrooms",
                    RESIDENTIAL_WEIGHTS["bedrooms"],
                    _difference_similarity(
                        target_building.bedrooms if target_building else None,
                        candidate_building.bedrooms if candidate_building else None,
                        [(0.0, 1.0), (1.0, 0.62), (2.0, 0.22), (3.0, 0.06), (4.0, 0.0)],
                    ),
                ),
                _component(
                    "bathrooms",
                    RESIDENTIAL_WEIGHTS["bathrooms"],
                    _difference_similarity(
                        target_building.bathrooms if target_building else None,
                        candidate_building.bathrooms if candidate_building else None,
                        [(0.0, 1.0), (0.5, 0.76), (1.0, 0.40), (1.5, 0.14), (2.5, 0.0)],
                    ),
                ),
                _component(
                    "quality",
                    RESIDENTIAL_WEIGHTS["quality"],
                    _quality_similarity(target_account.class_code, candidate_account.class_code),
                ),
                _component(
                    "age",
                    RESIDENTIAL_WEIGHTS["age"],
                    _difference_similarity(
                        _effective_year(target_improvement, target_account),
                        _effective_year(candidate_improvement, candidate_account),
                        [
                            (0.0, 1.0),
                            (2.0, 0.90),
                            (5.0, 0.76),
                            (10.0, 0.42),
                            (15.0, 0.22),
                            (25.0, 0.08),
                            (40.0, 0.0),
                        ],
                    ),
                ),
                _component(
                    "stories",
                    RESIDENTIAL_WEIGHTS["stories"],
                    _difference_similarity(
                        target_stories,
                        candidate_stories,
                        [(0.0, 1.0), (0.5, 0.70), (1.0, 0.35), (2.0, 0.0)],
                    ),
                ),
                _component(
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
            _component(
                "land_size",
                land_weight,
                _percentage_similarity(
                    target_acreage,
                    candidate_acreage,
                    [
                        (0.0, 1.0),
                        (0.05, 0.90),
                        (0.10, 0.76),
                        (0.20, 0.54),
                        (0.35, 0.28),
                        (0.50, 0.12),
                        (0.80, 0.0),
                    ],
                ),
            ),
            _component(
                "features", feature_weight, _feature_similarity(target_features, candidate_features)
            ),
            _component(
                "distance", distance_weight, _distance_similarity(distance, max_distance_miles)
            ),
        ]
    )

    return _score_from_components(components, is_land_only=is_land_only)


def find_similar_properties(
    prop_id: str,
    max_distance_miles: float = 10.0,
    max_results: int = 50,
    min_score: float = 30.0,
) -> list[dict]:
    """Find Brazos properties similar to the given prop_id. Mirrors
    data/similarity.py::find_similar_properties's DB-side haversine
    bounding-box approach against ParcelGeometry, then joins back to
    PropertyAccount by prop_id."""
    from counties.common.tax_models import ParcelGeometry

    target = PropertyAccount.objects.order_by("-tax_year").filter(prop_id=prop_id).first()
    if not target:
        return []

    target_geom = ParcelGeometry.objects.filter(account_number=prop_id, county="brazos").first()
    if not target_geom or not target_geom.latitude or not target_geom.longitude:
        return []

    tax_year = target.tax_year
    target_lat = float(target_geom.latitude)
    target_lon = float(target_geom.longitude)

    target_improvement, target_building = _primary_improvement(prop_id, tax_year)
    target_features = list(PropertyExtraFeature.objects.filter(prop_id=prop_id, tax_year=tax_year))
    target_acreage = _total_acreage(prop_id, tax_year)

    lat_range = max_distance_miles / 69.0
    lon_range = max_distance_miles / (69.0 * cos(radians(target_lat)))
    min_lat, max_lat = target_lat - lat_range, target_lat + lat_range
    min_lon, max_lon = target_lon - lon_range, target_lon + lon_range

    geom_candidates = ParcelGeometry.objects.filter(
        county="brazos",
        latitude__gte=min_lat,
        latitude__lte=max_lat,
        longitude__gte=min_lon,
        longitude__lte=max_lon,
        latitude__isnull=False,
        longitude__isnull=False,
    ).exclude(account_number=prop_id)

    # Restrict to parcels with a PropertyAccount row for this year, before the
    # cap below rather than after. ParcelGeometry is keyed by prop_id alone --
    # it carries no tax_year and holds every parcel in the shapefile -- so
    # without this, geometry for a parcel absent from `tax_year` would consume
    # a candidate slot and then vanish at the join, shrinking the comp pool.
    geom_candidates = geom_candidates.filter(
        account_number__in=PropertyAccount.objects.filter(tax_year=tax_year).values("prop_id")
    )

    target_lat_rad = radians(target_lat)
    target_lon_rad = radians(target_lon)

    geom_candidates = (
        geom_candidates.annotate(
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
        .order_by("distance")[:MAX_GEOMETRY_CANDIDATES]
    )

    geom_list = list(geom_candidates.values("account_number", "distance"))
    if not geom_list:
        return []

    candidate_prop_ids = [g["account_number"] for g in geom_list]
    distance_map = {g["account_number"]: g["distance"] for g in geom_list}

    candidate_list = list(
        PropertyAccount.objects.filter(prop_id__in=candidate_prop_ids, tax_year=tax_year)
    )
    if not candidate_list:
        return []

    improvements_by_prop: dict[str, list[PropertyImprovement]] = defaultdict(list)
    for imp in PropertyImprovement.objects.filter(
        prop_id__in=candidate_prop_ids, tax_year=tax_year, improvement_type="R"
    ).order_by("imp_id"):
        improvements_by_prop[imp.prop_id].append(imp)

    characteristics_by_imp: dict[str, PropertyBuildingCharacteristic] = {
        c.imp_id: c
        for c in PropertyBuildingCharacteristic.objects.filter(
            prop_id__in=candidate_prop_ids, tax_year=tax_year
        )
    }

    features_by_prop: dict[str, list[PropertyExtraFeature]] = defaultdict(list)
    for f in PropertyExtraFeature.objects.filter(prop_id__in=candidate_prop_ids, tax_year=tax_year):
        features_by_prop[f.prop_id].append(f)

    acreage_by_prop: dict[str, float] = defaultdict(float)
    has_land = set()
    for land_prop_id, acreage in PropertyLand.objects.filter(
        prop_id__in=candidate_prop_ids, tax_year=tax_year
    ).values_list("prop_id", "acreage"):
        if acreage is not None:
            acreage_by_prop[land_prop_id] += float(acreage)
            has_land.add(land_prop_id)

    results = []
    for candidate in candidate_list:
        dist = distance_map.get(candidate.prop_id, 0.0)

        c_improvements = improvements_by_prop.get(candidate.prop_id, [])
        c_improvement = None
        c_building = None
        for imp in c_improvements:
            characteristic = characteristics_by_imp.get(imp.imp_id)
            if characteristic is not None:
                c_improvement, c_building = imp, characteristic
                break
        if c_improvement is None and c_improvements:
            c_improvement = c_improvements[0]

        c_features = features_by_prop.get(candidate.prop_id, [])
        c_acreage = (
            acreage_by_prop.get(candidate.prop_id) if candidate.prop_id in has_land else None
        )

        details = calculate_similarity_details(
            target,
            candidate,
            target_improvement,
            c_improvement,
            target_building,
            c_building,
            target_features,
            c_features,
            target_acreage,
            c_acreage,
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
                    "acreage": c_acreage,
                    "distance": round(dist, 2),
                    "similarity_score": score,
                    "score_breakdown": details["components"],
                }
            )

    # Rank by score (then distance) before truncating to max_results, so the
    # candidates that get cut are the weakest matches, not just the farthest
    # ones enumerated first. This ordering is for *selecting* the top N, not
    # for how they're displayed — the shared web layer's
    # ``sort_comps_for_display`` owns final page order.
    results.sort(key=lambda x: (-x["similarity_score"], x["distance"], x["property"].prop_id))
    return results[:max_results]
