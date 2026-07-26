"""Similarity search for comparable properties.

Scores on living area, land size, room counts, quality, age and distance. The
algorithm is county-neutral: it operates on `ComparableProperty` from
`data.comparables`, which is where each district's models are mapped onto a
common shape. Supporting a new appraisal district means adding a source there,
not editing the scoring here.

Factors a district does not publish are skipped rather than zeroed, and the score
is renormalised over what remains — see `_score_from_components`.
"""

from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from math import asin, cos, radians, sin, sqrt

from .comparables import ComparableProperty, hcad_comparable, resolve_source
from .models import BuildingDetail, ExtraFeature, PropertyRecord

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
    target_codes: Sequence[str],
    candidate_codes: Sequence[str],
) -> float | None:
    """Compare building type/style/class, most specific code first."""
    for target_code, candidate_code in zip(target_codes, candidate_codes):
        similarity = _categorical_similarity(target_code, candidate_code)
        if similarity is not None:
            return similarity

    return None


def _grade_rank(code: str) -> int | None:
    """Extract an ordinal grade from a class code.

    HCAD grades with letters (A/B/C); Brazos embeds a digit in a structured code
    (`RV3`, `RV4P`, `RF2` — residential, grade 3/4/2). Without this the two
    counties' quality codes would only ever compare as equal-or-unknown.
    """
    digits = "".join(ch for ch in code if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits[:1])
    except ValueError:
        return None


def _quality_similarity(target_code: object, candidate_code: object) -> float | None:
    """Compare quality codes, whatever scheme the district uses."""
    ranked = _ranked_code_similarity(target_code, candidate_code, QUALITY_RANK)
    if ranked is not None:
        return ranked

    normalized_target = _normalized_code(target_code)
    normalized_candidate = _normalized_code(candidate_code)
    if not normalized_target or not normalized_candidate:
        return None

    target_rank = _grade_rank(normalized_target)
    candidate_rank = _grade_rank(normalized_candidate)
    if target_rank is not None and candidate_rank is not None:
        # A grade gap matters more when the code families also differ
        # ("RV3" vs "RF3" is a different construction class at the same grade).
        grade_similarity = _clamp(
            _interpolate_curve(
                abs(target_rank - candidate_rank),
                [(0.0, 1.0), (1.0, 0.72), (2.0, 0.42), (3.0, 0.18), (5.0, 0.0)],
            )
        )
        family_target = normalized_target.rstrip("0123456789P")
        family_candidate = normalized_candidate.rstrip("0123456789P")
        if family_target and family_candidate and family_target != family_candidate:
            grade_similarity *= 0.8
        return _clamp(grade_similarity)

    return _categorical_similarity(normalized_target, normalized_candidate)


def _feature_similarity(
    target_codes: AbstractSet[str] | None,
    candidate_codes: AbstractSet[str] | None,
) -> float | None:
    """Jaccard overlap of feature codes (pools, garages, porches)."""
    if target_codes is None or candidate_codes is None:
        return None

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
    components: list[dict[str, object]],
    *,
    is_land_only: bool,
) -> dict[str, object]:
    total_possible_weight = sum(float(component["weight"]) for component in components)
    available_components = [
        (float(component["weight"]), float(component["similarity"]))
        for component in components
        if component["similarity"] is not None
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


def score_comparables(
    target: ComparableProperty,
    candidate: ComparableProperty,
    distance: float = 0.0,
    max_distance_miles: float = 10.0,
) -> dict[str, object]:
    """Score two normalised properties, whatever county they came from.

    This is the algorithm proper. Anything county-specific belongs in
    `data.comparables`, which maps a district's models onto ComparableProperty.

    Factors a district does not publish arrive as None and are simply skipped:
    Brazos has no room counts or condition ratings, so its scores are built from
    the remaining factors and carry the usual completeness penalty. Rankings stay
    meaningful because every candidate in a search shares the same gaps, but an
    absolute score is only comparable within one county.
    """
    components: list[dict[str, object]] = []
    is_land_only = not target.has_building and not candidate.has_building

    if not is_land_only:
        components.extend(
            [
                _component(
                    "living_area",
                    RESIDENTIAL_WEIGHTS["living_area"],
                    _percentage_similarity(
                        target.living_area,
                        candidate.living_area,
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
                        target.bedrooms,
                        candidate.bedrooms,
                        [(0.0, 1.0), (1.0, 0.62), (2.0, 0.22), (3.0, 0.06), (4.0, 0.0)],
                    ),
                ),
                _component(
                    "bathrooms",
                    RESIDENTIAL_WEIGHTS["bathrooms"],
                    _difference_similarity(
                        target.bathrooms,
                        candidate.bathrooms,
                        [(0.0, 1.0), (0.5, 0.76), (1.0, 0.40), (1.5, 0.14), (2.5, 0.0)],
                    ),
                ),
                _component(
                    "quality",
                    RESIDENTIAL_WEIGHTS["quality"],
                    _quality_similarity(target.quality_code, candidate.quality_code),
                ),
                _component(
                    "condition",
                    RESIDENTIAL_WEIGHTS["condition"],
                    _condition_similarity(
                        target.condition_code,
                        candidate.condition_code,
                    ),
                ),
                _component(
                    "age",
                    RESIDENTIAL_WEIGHTS["age"],
                    _difference_similarity(
                        target.effective_year,
                        candidate.effective_year,
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
                        target.stories,
                        candidate.stories,
                        [(0.0, 1.0), (0.5, 0.70), (1.0, 0.35), (2.0, 0.0)],
                    ),
                ),
                _component(
                    "building_character",
                    RESIDENTIAL_WEIGHTS["building_character"],
                    _building_character_similarity(
                        target.character_codes, candidate.character_codes
                    ),
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
                    target.land_area,
                    candidate.land_area,
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
                "features",
                feature_weight,
                _feature_similarity(target.feature_codes, candidate.feature_codes),
            ),
            _component(
                "distance",
                distance_weight,
                _distance_similarity(distance, max_distance_miles),
            ),
        ]
    )

    return _score_from_components(components, is_land_only=is_land_only)


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
    """Calculate an explainable similarity score for two HCAD properties.

    Retained as the HCAD-shaped entry point; `score_comparables` is the
    county-neutral core.
    """
    return score_comparables(
        hcad_comparable(target_prop, target_building, target_features),
        hcad_comparable(candidate_prop, candidate_building, candidate_features),
        distance,
        max_distance_miles,
    )


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
    """Calculate a similarity score between two properties (0-100)."""
    details = calculate_similarity_details(
        target_prop,
        candidate_prop,
        target_building,
        candidate_building,
        target_features,
        candidate_features,
        distance,
        max_distance_miles,
    )
    return float(details["score"])


def find_similar_properties(
    account_number: str,
    max_distance_miles: float = 10.0,
    max_results: int = 50,
    min_score: float = 30.0,
    source: str | None = None,
) -> list[dict]:
    """Find properties comparable to the given account.

    `source` selects the county ("hcad" or "brazos"); when omitted the account is
    looked up in each in turn, HCAD first. Distance filtering happens in the
    database via a bounding box plus a great-circle distance, then scoring runs
    in Python over the candidates that survive.

    Each result keeps the underlying model instance under "property", so callers
    retain access to county-specific fields.
    """
    try:
        resolved = resolve_source(account_number, source)
    except ValueError:
        raise
    except Exception:
        return []

    if resolved is None:
        return []

    provider, target = resolved

    # Without coordinates there is nothing to search around.
    if not target.has_location:
        return []

    candidates = provider.find_candidates(target, max_distance_miles)
    if not candidates:
        return []

    results = []
    for candidate, distance in candidates:
        details = score_comparables(
            target,
            candidate,
            distance,
            max_distance_miles=max_distance_miles,
        )
        score = float(details["score"])
        if score < min_score:
            continue

        results.append(
            {
                "property": candidate.source,
                "comparable": candidate,
                "building": candidate.building,
                "features": list(candidate.features),
                "distance": round(distance, 2),
                "similarity_score": score,
                "score_breakdown": details["components"],
                "source": provider.name,
            }
        )

    results.sort(key=lambda x: (-x["similarity_score"], x["distance"], x["comparable"].key))
    return results[:max_results]


def _feature_code(feature: object) -> str:
    """Feature code from either county's row type."""
    for attr in ("feature_code", "imprv_det_type_cd"):
        if code := getattr(feature, attr, None):
            return str(code)
    return ""


def _feature_description(feature: object) -> str:
    """Human-readable feature label from either county's row type."""
    for attr in ("feature_description", "imprv_det_type_desc"):
        if description := getattr(feature, attr, None):
            return str(description)
    return _feature_code(feature)


def get_feature_summary(features: list[ExtraFeature]) -> dict[str, int]:
    """
    Get a summary of features by category.

    Returns:
        Dictionary with feature counts: {'POOL': 1, 'DETGAR': 2, ...}
    """
    summary: dict[str, int] = {}
    for feature in features:
        code = _feature_code(feature)
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
    feature_counts: dict[str, int] = {}
    for feature in features:
        desc = _feature_description(feature) or "Unknown"
        feature_counts[desc] = feature_counts.get(desc, 0) + 1

    # Format as readable list
    items = []
    for desc, count in sorted(feature_counts.items())[:max_features]:
        if count > 1:
            items.append(f"{desc} ({count})")
        else:
            items.append(desc)

    return ", ".join(items) if items else "None"
