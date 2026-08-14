"""Shared pure-math helpers for similarity scoring.

Both ``counties/harris/similarity.py`` and ``counties/brazos/similarity.py``
use the same curve interpolation, percentage/difference similarity, and
distance-curve functions.  These have zero model coupling — they take
numbers and return numbers — so they live here once rather than being
duplicated across the two county modules.

The ``component`` and ``score_from_components`` functions build the
score-breakdown dict shape that the shared web layer's
``ScoreComponent.from_mapping()`` (in ``contracts.py``) consumes.

The curves themselves (the ``list[tuple[float, float]]`` arguments) are
defined inline in each county's ``calculate_similarity_details`` — they
are per-factor tuning, not shared infrastructure.
"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


def clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    """Clamp a value to [lower, upper]."""
    return max(lower, min(upper, value))


def safe_float(value: object) -> float | None:
    """Coerce a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalized_code(value: object) -> str:
    """Normalize a code field for comparison: stripped + uppercased."""
    return str(value or "").strip().upper()


# ---------------------------------------------------------------------------
# Curve interpolation
# ---------------------------------------------------------------------------


def interpolate_curve(value: float, curve: list[tuple[float, float]]) -> float:
    """Return a smoothed similarity value from a piecewise linear curve.

    ``curve`` is a list of ``(x, y)`` points sorted by x.  For ``value``
    below the first x or above the last x, the first/last y is returned.
    Between points, linear interpolation is used.
    """
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


# ---------------------------------------------------------------------------
# Similarity functions (pure math — no model coupling)
# ---------------------------------------------------------------------------


def percentage_similarity(
    target_value: object,
    candidate_value: object,
    curve: list[tuple[float, float]],
) -> float | None:
    """Similarity based on percentage difference from target.

    Returns None when either value is missing or the target is zero/negative.
    """
    target_num = safe_float(target_value)
    candidate_num = safe_float(candidate_value)

    if target_num is None or candidate_num is None or target_num <= 0:
        return None

    diff_pct = abs(target_num - candidate_num) / target_num
    return clamp(interpolate_curve(diff_pct, curve))


def difference_similarity(
    target_value: object,
    candidate_value: object,
    curve: list[tuple[float, float]],
) -> float | None:
    """Similarity based on absolute difference."""
    target_num = safe_float(target_value)
    candidate_num = safe_float(candidate_value)

    if target_num is None or candidate_num is None:
        return None

    return clamp(interpolate_curve(abs(target_num - candidate_num), curve))


def categorical_similarity(target_code: object, candidate_code: object) -> float | None:
    """Similarity for categorical codes: exact > prefix-2 > prefix-1 > 0."""
    normalized_target = normalized_code(target_code)
    normalized_candidate = normalized_code(candidate_code)

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


def ranked_code_similarity(
    target_code: object,
    candidate_code: object,
    rank_map: dict[str, int],
) -> float | None:
    """Similarity for ranked codes (e.g. quality grades A-F).

    Uses a fixed curve: rank diff 0→1.0, 1→0.72, 2→0.42, 3→0.18, 5→0.0.
    Falls back to ``categorical_similarity`` when codes aren't in the map.
    """
    normalized_target = normalized_code(target_code)
    normalized_candidate = normalized_code(candidate_code)

    if not normalized_target or not normalized_candidate:
        return None

    if normalized_target == normalized_candidate:
        return 1.0

    target_rank = rank_map.get(normalized_target)
    candidate_rank = rank_map.get(normalized_candidate)

    if target_rank is None or candidate_rank is None:
        return None

    return clamp(
        interpolate_curve(
            abs(target_rank - candidate_rank),
            [(0.0, 1.0), (1.0, 0.72), (2.0, 0.42), (3.0, 0.18), (5.0, 0.0)],
        )
    )


def distance_similarity(distance: float, max_distance_miles: float) -> float | None:
    """Similarity based on distance as a fraction of the max search radius."""
    if max_distance_miles <= 0:
        return None

    ratio = clamp(distance / max_distance_miles)
    return clamp(
        interpolate_curve(
            ratio,
            [(0.0, 1.0), (0.1, 0.93), (0.25, 0.78), (0.5, 0.52), (0.75, 0.24), (1.0, 0.05)],
        )
    )


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points on Earth, in miles."""
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * asin(sqrt(a))
    return 3959 * c


# ---------------------------------------------------------------------------
# Score labels
# ---------------------------------------------------------------------------


def get_similarity_label(score: float) -> str:
    """Return a user-facing label for a 0-100 match score.

    The bands are shared across all counties — they describe the score
    itself, not anything county-specific (see CLAUDE.md's Similarity
    Algorithm section).
    """
    if score >= 84:
        return "Best match"
    if score >= 70:
        return "Highly similar"
    if score >= 52:
        return "Good match"
    if score >= 36:
        return "OK match"
    return "Broad match"


# ---------------------------------------------------------------------------
# Component / score assembly
# ---------------------------------------------------------------------------


def component(
    name: str,
    weight: float,
    similarity: float | None,
    labels: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build a score-breakdown component dict.

    ``labels`` maps component names to display labels; if omitted or the
    name is not in the map, a title-cased version of the name is used.
    """
    if labels is not None:
        label = labels.get(name, name.replace("_", " ").title())
    else:
        label = name.replace("_", " ").title()

    return {
        "name": name,
        "label": label,
        "weight": weight,
        "similarity": None if similarity is None else round(similarity, 3),
        "points": None if similarity is None else round(weight * similarity, 1),
        "available": similarity is not None,
    }


def score_from_components(
    components: list[dict[str, object]],
    *,
    is_land_only: bool,
) -> dict[str, object]:
    """Assemble a final score from a list of component dicts.

    Applies a completeness multiplier for non-land-only properties: when
    some components are unavailable (missing data), the score is scaled down
    to reflect reduced confidence. Land-only properties skip this multiplier
    because the three available components are always expected to be present.
    """
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
        "score": round(clamp(final_score, lower=0.0, upper=100.0), 1),
        "components": components,
        "available_weight": round(available_weight, 1),
    }
