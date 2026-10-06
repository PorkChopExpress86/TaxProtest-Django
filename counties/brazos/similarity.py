"""Similarity search for comparable Brazos properties.

The pure similarity math (numeric helpers, tuning curves, per-factor
similarity functions, label bands and score assembly) is shared with Harris
in counties/common/similarity_math.py (ADR-0021). This module owns what is
Brazos-specific: the factor list and weights, the class-code quality tier,
the SECOND-FLOOR stories proxy, primary-improvement selection, candidate
pre-filters, and the pair scorer.

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

from counties.common.similarity_math import (
    AGE_CURVE,
    BATHROOMS_CURVE,
    BEDROOMS_CURVE,
    LAND_SIZE_CURVE,
    LIVING_AREA_CURVE,
    RANK_DIFFERENCE_CURVE,
    STORIES_CURVE,
    categorical_similarity,
    clamp,
    component,
    difference_similarity,
    distance_similarity,
    interpolate_curve,
    nearby_properties,
    normalized_code,
    percentage_similarity,
    score_from_components,
)

from .models import (
    PropertyAccount,
    PropertyBuildingCharacteristic,
    PropertyExtraFeature,
    PropertyImprovement,
    PropertyImprovementDetail,
    PropertyLand,
)

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


# ---------------------------------------------------------------- Brazos-specific helpers


def _quality_digit(class_code: object) -> int | None:
    match = CLASS_CODE_DIGIT_RE.search(normalized_code(class_code))
    return int(match.group(1)) if match else None


def _quality_similarity(target_class_code: object, candidate_class_code: object) -> float | None:
    target_digit = _quality_digit(target_class_code)
    candidate_digit = _quality_digit(candidate_class_code)
    if target_digit is None or candidate_digit is None:
        return None
    return clamp(interpolate_curve(abs(target_digit - candidate_digit), RANK_DIFFERENCE_CURVE))


def _building_character_similarity(
    target_building: PropertyBuildingCharacteristic | None,
    candidate_building: PropertyBuildingCharacteristic | None,
) -> float | None:
    if target_building is None or candidate_building is None:
        return None
    for attr_name in ("exterior_wall", "construction_style", "foundation"):
        similarity = categorical_similarity(
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
                component(
                    "living_area",
                    RESIDENTIAL_WEIGHTS["living_area"],
                    percentage_similarity(
                        target_account.living_area,
                        candidate_account.living_area,
                        LIVING_AREA_CURVE,
                    ),
                    labels=COMPONENT_LABELS,
                ),
                component(
                    "bedrooms",
                    RESIDENTIAL_WEIGHTS["bedrooms"],
                    difference_similarity(
                        target_building.bedrooms if target_building else None,
                        candidate_building.bedrooms if candidate_building else None,
                        BEDROOMS_CURVE,
                    ),
                    labels=COMPONENT_LABELS,
                ),
                component(
                    "bathrooms",
                    RESIDENTIAL_WEIGHTS["bathrooms"],
                    difference_similarity(
                        target_building.bathrooms if target_building else None,
                        candidate_building.bathrooms if candidate_building else None,
                        BATHROOMS_CURVE,
                    ),
                    labels=COMPONENT_LABELS,
                ),
                component(
                    "quality",
                    RESIDENTIAL_WEIGHTS["quality"],
                    _quality_similarity(target_account.class_code, candidate_account.class_code),
                    labels=COMPONENT_LABELS,
                ),
                component(
                    "age",
                    RESIDENTIAL_WEIGHTS["age"],
                    difference_similarity(
                        _effective_year(target_improvement, target_account),
                        _effective_year(candidate_improvement, candidate_account),
                        AGE_CURVE,
                    ),
                    labels=COMPONENT_LABELS,
                ),
                component(
                    "stories",
                    RESIDENTIAL_WEIGHTS["stories"],
                    difference_similarity(
                        target_stories,
                        candidate_stories,
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
                    target_acreage,
                    candidate_acreage,
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
    prop_id: str,
    *,
    tax_year: int,
    max_distance_miles: float = 10.0,
    max_results: int = 50,
    min_score: float = 30.0,
) -> list[dict]:
    """Find Brazos properties similar to the given prop_id.

    Candidates come from the shared nearest-properties query, pre-filtered
    here to the same tax year."""
    target = PropertyAccount.objects.filter(prop_id=prop_id, tax_year=tax_year).first()
    if not target or not target.latitude or not target.longitude:
        return []
    target_lat = float(target.latitude)
    target_lon = float(target.longitude)

    target_improvement, target_building = _primary_improvement(prop_id, tax_year)
    target_features = list(PropertyExtraFeature.objects.filter(prop_id=prop_id, tax_year=tax_year))
    target_acreage = _total_acreage(prop_id, tax_year)

    candidates = nearby_properties(
        PropertyAccount.objects.filter(tax_year=tax_year).exclude(prop_id=prop_id),
        latitude=target_lat,
        longitude=target_lon,
        max_distance_miles=max_distance_miles,
    )

    candidate_list = list(candidates)
    if not candidate_list:
        return []

    candidate_prop_ids = [c.prop_id for c in candidate_list]

    improvements_by_prop: dict[str, list[PropertyImprovement]] = defaultdict(list)
    for imp in PropertyImprovement.objects.filter(
        prop_id__in=candidate_prop_ids, tax_year=tax_year, improvement_type="R"
    ).order_by("imp_id"):
        improvements_by_prop[imp.prop_id].append(imp)

    # Keyed by (prop_id, imp_id): imp_id alone repeats across properties.
    characteristics_by_imp: dict[tuple[str, str], PropertyBuildingCharacteristic] = {
        (c.prop_id, c.imp_id): c
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
        dist = getattr(candidate, "distance", 0.0)

        c_improvements = improvements_by_prop.get(candidate.prop_id, [])
        c_improvement = None
        c_building = None
        for imp in c_improvements:
            characteristic = characteristics_by_imp.get((imp.prop_id, imp.imp_id))
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
