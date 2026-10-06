"""Shared similarity math used by every county's similarity scoring.

The math takes numbers or codes and returns numbers or dicts, with no model
coupling: numeric helpers, curve interpolation, the per-factor similarity
functions, the tuning curves as named constants, the user-facing label bands,
and score assembly. ``nearby_properties`` is the one nearest-properties query:
it narrows any county queryset with latitude/longitude fields, so it names no
county model. Each county keeps its own factor list, weights, quality and
condition semantics, candidate pre-filters, and pair scorer (ADR-0021).

The tuning curves are shared. A county that needs a different curve defines
and justifies its own constant (ADR-0003) rather than editing these.

``component`` and ``score_from_components`` build the score-breakdown dict
shape that the shared web layer's ``ScoreComponent.from_mapping()`` (in
``contracts.py``) consumes.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import cos, radians

from django.db.models import ExpressionWrapper, F, FloatField, QuerySet, Value
from django.db.models.functions import ACos, Cos, Greatest, Least, Radians, Sin

Curve = Sequence[tuple[float, float]]

# ---------------------------------------------------------------------------
# Tuning curves: (x, similarity) points sorted by x
# ---------------------------------------------------------------------------

# x is the fractional difference from the target's living area.
LIVING_AREA_CURVE: Curve = (
    (0.0, 1.0),
    (0.03, 0.96),
    (0.05, 0.90),
    (0.10, 0.78),
    (0.20, 0.55),
    (0.30, 0.32),
    (0.40, 0.16),
    (0.50, 0.06),
    (0.75, 0.0),
)

# x is the fractional difference from the target's land size.
LAND_SIZE_CURVE: Curve = (
    (0.0, 1.0),
    (0.05, 0.90),
    (0.10, 0.76),
    (0.20, 0.54),
    (0.35, 0.28),
    (0.50, 0.12),
    (0.80, 0.0),
)

# x is the absolute difference in bedrooms.
BEDROOMS_CURVE: Curve = ((0.0, 1.0), (1.0, 0.62), (2.0, 0.22), (3.0, 0.06), (4.0, 0.0))

# x is the absolute difference in bathrooms.
BATHROOMS_CURVE: Curve = ((0.0, 1.0), (0.5, 0.76), (1.0, 0.40), (1.5, 0.14), (2.5, 0.0))

# x is the absolute difference in effective year, in years.
AGE_CURVE: Curve = (
    (0.0, 1.0),
    (2.0, 0.90),
    (5.0, 0.76),
    (10.0, 0.42),
    (15.0, 0.22),
    (25.0, 0.08),
    (40.0, 0.0),
)

# x is the absolute difference in stories.
STORIES_CURVE: Curve = ((0.0, 1.0), (0.5, 0.70), (1.0, 0.35), (2.0, 0.0))

# x is the absolute difference in rank between two ranked codes.
RANK_DIFFERENCE_CURVE: Curve = ((0.0, 1.0), (1.0, 0.72), (2.0, 0.42), (3.0, 0.18), (5.0, 0.0))

# x is the distance as a fraction of the search radius.
DISTANCE_CURVE: Curve = (
    (0.0, 1.0),
    (0.1, 0.93),
    (0.25, 0.78),
    (0.5, 0.52),
    (0.75, 0.24),
    (1.0, 0.05),
)

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


def interpolate_curve(value: float, curve: Curve) -> float:
    """Return a smoothed similarity value from a piecewise linear curve.

    ``curve`` is a sequence of ``(x, y)`` points sorted by x.  For ``value``
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
    curve: Curve,
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
    curve: Curve,
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

    Scores the rank difference on ``RANK_DIFFERENCE_CURVE``. Returns None
    when either code is missing from ``rank_map``.
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

    return clamp(interpolate_curve(abs(target_rank - candidate_rank), RANK_DIFFERENCE_CURVE))


def distance_similarity(distance: float, max_distance_miles: float) -> float | None:
    """Similarity based on distance as a fraction of the max search radius."""
    if max_distance_miles <= 0:
        return None

    ratio = clamp(distance / max_distance_miles)
    return clamp(interpolate_curve(ratio, DISTANCE_CURVE))


# ---------------------------------------------------------------------------
# Score labels
# ---------------------------------------------------------------------------


def get_similarity_label(score: float) -> str:
    """Return a user-facing label for a 0-100 match score.

    The bands are shared across all counties: they describe the score
    itself, not anything county-specific.
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


# ---------------------------------------------------------------------------
# Nearest-properties query
# ---------------------------------------------------------------------------

# Most candidates one search scores. Read at call time so tests can lower it.
NEARBY_PROPERTIES_CAP = 2000

EARTH_RADIUS_MILES = 3959.0


def nearby_properties(
    candidates: QuerySet,
    *,
    latitude: float,
    longitude: float,
    max_distance_miles: float,
) -> QuerySet:
    """Narrow pre-filtered ``candidates`` to the nearest ones within the radius.

    The county applies its own pre-filters (and excludes the subject) first.
    This adds a bounding box, the great-circle distance in miles as a
    ``distance`` annotation computed in the database, the radius filter,
    nearest-first ordering, and the ``NEARBY_PROPERTIES_CAP`` slice.
    """
    lat_range = max_distance_miles / 69.0
    lon_range = max_distance_miles / (69.0 * cos(radians(latitude)))
    target_lat_rad = radians(latitude)
    target_lon_rad = radians(longitude)

    return (
        candidates.filter(
            latitude__gte=latitude - lat_range,
            latitude__lte=latitude + lat_range,
            longitude__gte=longitude - lon_range,
            longitude__lte=longitude + lon_range,
            latitude__isnull=False,
            longitude__isnull=False,
        )
        .annotate(
            distance=ExpressionWrapper(
                EARTH_RADIUS_MILES
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
        .order_by("distance")[:NEARBY_PROPERTIES_CAP]
    )
