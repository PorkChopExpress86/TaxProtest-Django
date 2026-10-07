"""Golden similarity results for both counties.

These pin today's full ranked output of each county's public similarity search
and of each adapter's comparables lookup, so later similarity and readiness
changes can be proved score-neutral. The expected values were captured from
the implementation before any refactor; an intended scoring change must
update the matching golden in the same commit and say why.

Search rows are ``(key, distance, similarity_score, breakdown)`` with each
breakdown component as ``(name, label, weight, similarity, points,
available)``. Comparables rows are ``(key, distance, similarity_score,
match_label, bedrooms, bathrooms, breakdown)`` with each component as
``(name, label, weight, similarity, points)``.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from counties.brazos.adapter import adapter as brazos_adapter
from counties.brazos.models import PropertyImprovement
from counties.brazos.similarity import find_similar_properties as find_similar_brazos
from counties.common.tests.similarity_scenarios import (
    BRAZOS_TAX_YEAR,
    assert_brazos_improvement_ids_distinct,
    build_brazos_building_free_scenario,
    build_brazos_residential_scenario,
    build_harris_land_subject_scenario,
    build_harris_residential_scenario,
)
from counties.harris.adapter import adapter as harris_adapter
from counties.harris.similarity import find_similar_properties as find_similar_harris

LOOKUP = {"max_distance_miles": 10.0, "max_results": 50, "min_score": 30.0}


def ranked_search(results: list[dict], key_attr: str) -> list[tuple]:
    return [
        (
            getattr(result["property"], key_attr),
            result["distance"],
            result["similarity_score"],
            [
                (
                    component["name"],
                    component["label"],
                    component["weight"],
                    component["similarity"],
                    component["points"],
                    component["available"],
                )
                for component in result["score_breakdown"]
            ],
        )
        for result in results
    ]


def ranked_comps(comps) -> list[tuple]:
    return [
        (
            comp.key,
            comp.distance,
            comp.similarity_score,
            comp.match_label,
            comp.bedrooms,
            comp.bathrooms,
            [
                (part.name, part.label, part.weight, part.similarity, part.points)
                for part in comp.score_breakdown
            ],
        )
        for comp in comps
    ]


HR_SEARCH = [
    (
        "HC0000000001",
        0.14,
        97.2,
        [
            ("living_area", "Living Area", 24.0, 0.97, 23.3, True),
            ("bedrooms", "Bedrooms", 14.0, 1.0, 14.0, True),
            ("bathrooms", "Bathrooms", 12.0, 1.0, 12.0, True),
            ("quality", "Quality", 10.0, 1.0, 10.0, True),
            ("condition", "Condition", 6.0, 1.0, 6.0, True),
            ("age", "Age", 8.0, 1.0, 8.0, True),
            ("stories", "Stories", 4.0, 1.0, 4.0, True),
            ("building_character", "Building Type", 4.0, 1.0, 4.0, True),
            ("land_size", "Land Size", 10.0, 1.0, 10.0, True),
            ("features", "Features", 4.0, 0.5, 2.0, True),
            ("distance", "Distance", 4.0, 0.99, 4.0, True),
        ],
    ),
    (
        "HC0000000003",
        0.35,
        81.1,
        [
            ("living_area", "Living Area", 24.0, 0.696, 16.7, True),
            ("bedrooms", "Bedrooms", 14.0, 1.0, 14.0, True),
            ("bathrooms", "Bathrooms", 12.0, 1.0, 12.0, True),
            ("quality", "Quality", 10.0, 1.0, 10.0, True),
            ("condition", "Condition", 6.0, None, None, False),
            ("age", "Age", 8.0, 0.624, 5.0, True),
            ("stories", "Stories", 4.0, 1.0, 4.0, True),
            ("building_character", "Building Type", 4.0, 0.4, 1.6, True),
            ("land_size", "Land Size", 10.0, 1.0, 10.0, True),
            ("features", "Features", 4.0, 0.0, 0.0, True),
            ("distance", "Distance", 4.0, 0.976, 3.9, True),
        ],
    ),
    (
        "HC0000000004",
        0.35,
        81.1,
        [
            ("living_area", "Living Area", 24.0, 0.696, 16.7, True),
            ("bedrooms", "Bedrooms", 14.0, 1.0, 14.0, True),
            ("bathrooms", "Bathrooms", 12.0, 1.0, 12.0, True),
            ("quality", "Quality", 10.0, 1.0, 10.0, True),
            ("condition", "Condition", 6.0, None, None, False),
            ("age", "Age", 8.0, 0.624, 5.0, True),
            ("stories", "Stories", 4.0, 1.0, 4.0, True),
            ("building_character", "Building Type", 4.0, 0.4, 1.6, True),
            ("land_size", "Land Size", 10.0, 1.0, 10.0, True),
            ("features", "Features", 4.0, 0.0, 0.0, True),
            ("distance", "Distance", 4.0, 0.976, 3.9, True),
        ],
    ),
    (
        "HC0000000005",
        2.39,
        79.4,
        [
            ("living_area", "Living Area", 24.0, 0.78, 18.7, True),
            ("bedrooms", "Bedrooms", 14.0, 1.0, 14.0, True),
            ("bathrooms", "Bathrooms", 12.0, 0.76, 9.1, True),
            ("quality", "Quality", 10.0, None, None, False),
            ("condition", "Condition", 6.0, 0.4, 2.4, True),
            ("age", "Age", 8.0, 1.0, 8.0, True),
            ("stories", "Stories", 4.0, 0.7, 2.8, True),
            ("building_character", "Building Type", 4.0, 1.0, 4.0, True),
            ("land_size", "Land Size", 10.0, None, None, False),
            ("features", "Features", 4.0, 1.0, 4.0, True),
            ("distance", "Distance", 4.0, 0.791, 3.2, True),
        ],
    ),
    (
        "HC0000000002",
        0.91,
        54.6,
        [
            ("living_area", "Living Area", 24.0, 0.55, 13.2, True),
            ("bedrooms", "Bedrooms", 14.0, 0.62, 8.7, True),
            ("bathrooms", "Bathrooms", 12.0, 0.76, 9.1, True),
            ("quality", "Quality", 10.0, 0.72, 7.2, True),
            ("condition", "Condition", 6.0, 0.42, 2.5, True),
            ("age", "Age", 8.0, 0.34, 2.7, True),
            ("stories", "Stories", 4.0, 0.35, 1.4, True),
            ("building_character", "Building Type", 4.0, 0.4, 1.6, True),
            ("land_size", "Land Size", 10.0, 0.309, 3.1, True),
            ("features", "Features", 4.0, 0.333, 1.3, True),
            ("distance", "Distance", 4.0, 0.936, 3.7, True),
        ],
    ),
]

HR_COMPS = [
    (
        "HC0000000001",
        0.14,
        97.2,
        "Best match",
        4,
        Decimal("2.50"),
        [
            ("living_area", "Living Area", 24.0, 0.97, 23.3),
            ("bedrooms", "Bedrooms", 14.0, 1.0, 14.0),
            ("bathrooms", "Bathrooms", 12.0, 1.0, 12.0),
            ("quality", "Quality", 10.0, 1.0, 10.0),
            ("condition", "Condition", 6.0, 1.0, 6.0),
            ("age", "Age", 8.0, 1.0, 8.0),
            ("stories", "Stories", 4.0, 1.0, 4.0),
            ("building_character", "Building Type", 4.0, 1.0, 4.0),
            ("land_size", "Land Size", 10.0, 1.0, 10.0),
            ("features", "Features", 4.0, 0.5, 2.0),
            ("distance", "Distance", 4.0, 0.99, 4.0),
        ],
    ),
    (
        "HC0000000003",
        0.35,
        81.1,
        "Highly similar",
        4,
        Decimal("2.50"),
        [
            ("living_area", "Living Area", 24.0, 0.696, 16.7),
            ("bedrooms", "Bedrooms", 14.0, 1.0, 14.0),
            ("bathrooms", "Bathrooms", 12.0, 1.0, 12.0),
            ("quality", "Quality", 10.0, 1.0, 10.0),
            ("condition", "Condition", 6.0, None, None),
            ("age", "Age", 8.0, 0.624, 5.0),
            ("stories", "Stories", 4.0, 1.0, 4.0),
            ("building_character", "Building Type", 4.0, 0.4, 1.6),
            ("land_size", "Land Size", 10.0, 1.0, 10.0),
            ("features", "Features", 4.0, 0.0, 0.0),
            ("distance", "Distance", 4.0, 0.976, 3.9),
        ],
    ),
    (
        "HC0000000004",
        0.35,
        81.1,
        "Highly similar",
        4,
        Decimal("2.50"),
        [
            ("living_area", "Living Area", 24.0, 0.696, 16.7),
            ("bedrooms", "Bedrooms", 14.0, 1.0, 14.0),
            ("bathrooms", "Bathrooms", 12.0, 1.0, 12.0),
            ("quality", "Quality", 10.0, 1.0, 10.0),
            ("condition", "Condition", 6.0, None, None),
            ("age", "Age", 8.0, 0.624, 5.0),
            ("stories", "Stories", 4.0, 1.0, 4.0),
            ("building_character", "Building Type", 4.0, 0.4, 1.6),
            ("land_size", "Land Size", 10.0, 1.0, 10.0),
            ("features", "Features", 4.0, 0.0, 0.0),
            ("distance", "Distance", 4.0, 0.976, 3.9),
        ],
    ),
    (
        "HC0000000005",
        2.39,
        79.4,
        "Highly similar",
        4,
        Decimal("3.00"),
        [
            ("living_area", "Living Area", 24.0, 0.78, 18.7),
            ("bedrooms", "Bedrooms", 14.0, 1.0, 14.0),
            ("bathrooms", "Bathrooms", 12.0, 0.76, 9.1),
            ("quality", "Quality", 10.0, None, None),
            ("condition", "Condition", 6.0, 0.4, 2.4),
            ("age", "Age", 8.0, 1.0, 8.0),
            ("stories", "Stories", 4.0, 0.7, 2.8),
            ("building_character", "Building Type", 4.0, 1.0, 4.0),
            ("land_size", "Land Size", 10.0, None, None),
            ("features", "Features", 4.0, 1.0, 4.0),
            ("distance", "Distance", 4.0, 0.791, 3.2),
        ],
    ),
    (
        "HC0000000002",
        0.91,
        54.6,
        "Good match",
        3,
        Decimal("2.00"),
        [
            ("living_area", "Living Area", 24.0, 0.55, 13.2),
            ("bedrooms", "Bedrooms", 14.0, 0.62, 8.7),
            ("bathrooms", "Bathrooms", 12.0, 0.76, 9.1),
            ("quality", "Quality", 10.0, 0.72, 7.2),
            ("condition", "Condition", 6.0, 0.42, 2.5),
            ("age", "Age", 8.0, 0.34, 2.7),
            ("stories", "Stories", 4.0, 0.35, 1.4),
            ("building_character", "Building Type", 4.0, 0.4, 1.6),
            ("land_size", "Land Size", 10.0, 0.309, 3.1),
            ("features", "Features", 4.0, 0.333, 1.3),
            ("distance", "Distance", 4.0, 0.936, 3.7),
        ],
    ),
]

HL_SEARCH = [
    (
        "HL0000000004",
        4.15,
        84.1,
        [
            ("land_size", "Land Size", 80.0, 0.975, 78.0, True),
            ("features", "Features", 10.0, 0.0, 0.0, True),
            ("distance", "Distance", 10.0, 0.609, 6.1, True),
        ],
    ),
    (
        "HL0000000002",
        0.28,
        64.0,
        [
            ("land_size", "Land Size", 10.0, 0.76, 7.6, True),
            ("features", "Features", 4.0, 0.0, 0.0, True),
            ("distance", "Distance", 4.0, 0.981, 3.9, True),
        ],
    ),
    (
        "HL0000000003",
        1.04,
        59.7,
        [
            ("land_size", "Land Size", 80.0, 0.568, 45.4, True),
            ("features", "Features", 10.0, 0.5, 5.0, True),
            ("distance", "Distance", 10.0, 0.926, 9.3, True),
        ],
    ),
]

HL_COMPS = [
    (
        "HL0000000004",
        4.15,
        84.1,
        "Best match",
        None,
        None,
        [
            ("land_size", "Land Size", 80.0, 0.975, 78.0),
            ("features", "Features", 10.0, 0.0, 0.0),
            ("distance", "Distance", 10.0, 0.609, 6.1),
        ],
    ),
    (
        "HL0000000002",
        0.28,
        64.0,
        "Good match",
        4,
        Decimal("2.50"),
        [
            ("land_size", "Land Size", 10.0, 0.76, 7.6),
            ("features", "Features", 4.0, 0.0, 0.0),
            ("distance", "Distance", 4.0, 0.981, 3.9),
        ],
    ),
    (
        "HL0000000003",
        1.04,
        59.7,
        "Good match",
        None,
        None,
        [
            ("land_size", "Land Size", 80.0, 0.568, 45.4),
            ("features", "Features", 10.0, 0.5, 5.0),
            ("distance", "Distance", 10.0, 0.926, 9.3),
        ],
    ),
]

BR_SEARCH = [
    (
        "BC0000000004",
        0.21,
        99.9,
        [
            ("living_area", "Living Area", 24.0, 1.0, 24.0, True),
            ("bedrooms", "Bedrooms", 14.0, 1.0, 14.0, True),
            ("bathrooms", "Bathrooms", 12.0, 1.0, 12.0, True),
            ("quality", "Quality", 16.0, 1.0, 16.0, True),
            ("age", "Age", 8.0, 1.0, 8.0, True),
            ("stories", "Stories", 4.0, 1.0, 4.0, True),
            ("building_character", "Building Character", 4.0, 1.0, 4.0, True),
            ("land_size", "Land Size", 10.0, 1.0, 10.0, True),
            ("features", "Features", 4.0, 1.0, 4.0, True),
            ("distance", "Distance", 4.0, 0.985, 3.9, True),
        ],
    ),
    (
        "BC0000000001",
        0.14,
        94.6,
        [
            ("living_area", "Living Area", 24.0, 0.97, 23.3, True),
            ("bedrooms", "Bedrooms", 14.0, 1.0, 14.0, True),
            ("bathrooms", "Bathrooms", 12.0, 1.0, 12.0, True),
            ("quality", "Quality", 16.0, 1.0, 16.0, True),
            ("age", "Age", 8.0, 0.9, 7.2, True),
            ("stories", "Stories", 4.0, 1.0, 4.0, True),
            ("building_character", "Building Character", 4.0, 1.0, 4.0, True),
            ("land_size", "Land Size", 10.0, 0.816, 8.2, True),
            ("features", "Features", 4.0, 0.5, 2.0, True),
            ("distance", "Distance", 4.0, 0.99, 4.0, True),
        ],
    ),
    (
        "BC0000000005",
        0.41,
        77.2,
        [
            ("living_area", "Living Area", 24.0, None, None, False),
            ("bedrooms", "Bedrooms", 14.0, None, None, False),
            ("bathrooms", "Bathrooms", 12.0, None, None, False),
            ("quality", "Quality", 16.0, 1.0, 16.0, True),
            ("age", "Age", 8.0, 0.95, 7.6, True),
            ("stories", "Stories", 4.0, None, None, False),
            ("building_character", "Building Character", 4.0, None, None, False),
            ("land_size", "Land Size", 10.0, 0.92, 9.2, True),
            ("features", "Features", 4.0, 0.0, 0.0, True),
            ("distance", "Distance", 4.0, 0.971, 3.9, True),
        ],
    ),
    (
        "BC0000000006",
        1.64,
        69.5,
        [
            ("living_area", "Living Area", 24.0, 0.749, 18.0, True),
            ("bedrooms", "Bedrooms", 14.0, None, None, False),
            ("bathrooms", "Bathrooms", 12.0, None, None, False),
            ("quality", "Quality", 16.0, 1.0, 16.0, True),
            ("age", "Age", 8.0, 0.807, 6.5, True),
            ("stories", "Stories", 4.0, 0.35, 1.4, True),
            ("building_character", "Building Character", 4.0, None, None, False),
            ("land_size", "Land Size", 10.0, None, None, False),
            ("features", "Features", 4.0, 0.0, 0.0, True),
            ("distance", "Distance", 4.0, 0.866, 3.5, True),
        ],
    ),
    (
        "BC0000000003",
        0.35,
        67.7,
        [
            ("living_area", "Living Area", 24.0, 0.696, 16.7, True),
            ("bedrooms", "Bedrooms", 14.0, 0.62, 8.7, True),
            ("bathrooms", "Bathrooms", 12.0, 0.76, 9.1, True),
            ("quality", "Quality", 16.0, 0.72, 11.5, True),
            ("age", "Age", 8.0, 0.556, 4.4, True),
            ("stories", "Stories", 4.0, 1.0, 4.0, True),
            ("building_character", "Building Character", 4.0, None, None, False),
            ("land_size", "Land Size", 10.0, 0.716, 7.2, True),
            ("features", "Features", 4.0, 0.0, 0.0, True),
            ("distance", "Distance", 4.0, 0.976, 3.9, True),
        ],
    ),
    (
        "BC0000000002",
        0.91,
        54.1,
        [
            ("living_area", "Living Area", 24.0, 0.55, 13.2, True),
            ("bedrooms", "Bedrooms", 14.0, 0.62, 8.7, True),
            ("bathrooms", "Bathrooms", 12.0, 0.76, 9.1, True),
            ("quality", "Quality", 16.0, 0.72, 11.5, True),
            ("age", "Age", 8.0, 0.34, 2.7, True),
            ("stories", "Stories", 4.0, 0.35, 1.4, True),
            ("building_character", "Building Character", 4.0, 0.4, 1.6, True),
            ("land_size", "Land Size", 10.0, 0.08, 0.8, True),
            ("features", "Features", 4.0, 0.333, 1.3, True),
            ("distance", "Distance", 4.0, 0.936, 3.7, True),
        ],
    ),
]

BR_COMPS_50 = [
    (
        "BC0000000001",
        0.14,
        94.6,
        "Best match",
        4,
        Decimal("2.50"),
        [
            ("living_area", "Living Area", 24.0, 0.97, 23.3),
            ("bedrooms", "Bedrooms", 14.0, 1.0, 14.0),
            ("bathrooms", "Bathrooms", 12.0, 1.0, 12.0),
            ("quality", "Quality", 16.0, 1.0, 16.0),
            ("age", "Age", 8.0, 0.9, 7.2),
            ("stories", "Stories", 4.0, 1.0, 4.0),
            ("building_character", "Building Character", 4.0, 1.0, 4.0),
            ("land_size", "Land Size", 10.0, 0.816, 8.2),
            ("features", "Features", 4.0, 0.5, 2.0),
            ("distance", "Distance", 4.0, 0.99, 4.0),
        ],
    ),
    (
        "BC0000000006",
        1.64,
        69.5,
        "Good match",
        None,
        None,
        [
            ("living_area", "Living Area", 24.0, 0.749, 18.0),
            ("bedrooms", "Bedrooms", 14.0, None, None),
            ("bathrooms", "Bathrooms", 12.0, None, None),
            ("quality", "Quality", 16.0, 1.0, 16.0),
            ("age", "Age", 8.0, 0.807, 6.5),
            ("stories", "Stories", 4.0, 0.35, 1.4),
            ("building_character", "Building Character", 4.0, None, None),
            ("land_size", "Land Size", 10.0, None, None),
            ("features", "Features", 4.0, 0.0, 0.0),
            ("distance", "Distance", 4.0, 0.866, 3.5),
        ],
    ),
    (
        "BC0000000003",
        0.35,
        67.7,
        "Good match",
        3,
        Decimal("2.00"),
        [
            ("living_area", "Living Area", 24.0, 0.696, 16.7),
            ("bedrooms", "Bedrooms", 14.0, 0.62, 8.7),
            ("bathrooms", "Bathrooms", 12.0, 0.76, 9.1),
            ("quality", "Quality", 16.0, 0.72, 11.5),
            ("age", "Age", 8.0, 0.556, 4.4),
            ("stories", "Stories", 4.0, 1.0, 4.0),
            ("building_character", "Building Character", 4.0, None, None),
            ("land_size", "Land Size", 10.0, 0.716, 7.2),
            ("features", "Features", 4.0, 0.0, 0.0),
            ("distance", "Distance", 4.0, 0.976, 3.9),
        ],
    ),
    (
        "BC0000000002",
        0.91,
        54.1,
        "Good match",
        3,
        Decimal("2.00"),
        [
            ("living_area", "Living Area", 24.0, 0.55, 13.2),
            ("bedrooms", "Bedrooms", 14.0, 0.62, 8.7),
            ("bathrooms", "Bathrooms", 12.0, 0.76, 9.1),
            ("quality", "Quality", 16.0, 0.72, 11.5),
            ("age", "Age", 8.0, 0.34, 2.7),
            ("stories", "Stories", 4.0, 0.35, 1.4),
            ("building_character", "Building Character", 4.0, 0.4, 1.6),
            ("land_size", "Land Size", 10.0, 0.08, 0.8),
            ("features", "Features", 4.0, 0.333, 1.3),
            ("distance", "Distance", 4.0, 0.936, 3.7),
        ],
    ),
]

BR_COMPS_2 = [
    (
        "BC0000000001",
        0.14,
        94.6,
        "Best match",
        4,
        Decimal("2.50"),
        [
            ("living_area", "Living Area", 24.0, 0.97, 23.3),
            ("bedrooms", "Bedrooms", 14.0, 1.0, 14.0),
            ("bathrooms", "Bathrooms", 12.0, 1.0, 12.0),
            ("quality", "Quality", 16.0, 1.0, 16.0),
            ("age", "Age", 8.0, 0.9, 7.2),
            ("stories", "Stories", 4.0, 1.0, 4.0),
            ("building_character", "Building Character", 4.0, 1.0, 4.0),
            ("land_size", "Land Size", 10.0, 0.816, 8.2),
            ("features", "Features", 4.0, 0.5, 2.0),
            ("distance", "Distance", 4.0, 0.99, 4.0),
        ],
    )
]

BF_SEARCH = [
    (
        "BF0000000003",
        0.83,
        89.4,
        [
            ("land_size", "Land Size", 80.0, 1.0, 80.0, True),
            ("features", "Features", 10.0, 0.0, 0.0, True),
            ("distance", "Distance", 10.0, 0.942, 9.4, True),
        ],
    ),
    (
        "BF0000000004",
        0.55,
        73.4,
        [
            ("living_area", "Living Area", 24.0, 0.914, 21.9, True),
            ("bedrooms", "Bedrooms", 14.0, None, None, False),
            ("bathrooms", "Bathrooms", 12.0, None, None, False),
            ("quality", "Quality", 16.0, 1.0, 16.0, True),
            ("age", "Age", 8.0, 0.76, 6.1, True),
            ("stories", "Stories", 4.0, 0.35, 1.4, True),
            ("building_character", "Building Character", 4.0, None, None, False),
            ("land_size", "Land Size", 10.0, 0.54, 5.4, True),
            ("features", "Features", 4.0, 0.0, 0.0, True),
            ("distance", "Distance", 4.0, 0.961, 3.8, True),
        ],
    ),
    (
        "BF0000000002",
        0.28,
        52.5,
        [
            ("land_size", "Land Size", 80.0, 0.471, 37.7, True),
            ("features", "Features", 10.0, 0.5, 5.0, True),
            ("distance", "Distance", 10.0, 0.981, 9.8, True),
        ],
    ),
]

BF_COMPS = [
    (
        "BF0000000003",
        0.83,
        89.4,
        "Best match",
        None,
        None,
        [
            ("land_size", "Land Size", 80.0, 1.0, 80.0),
            ("features", "Features", 10.0, 0.0, 0.0),
            ("distance", "Distance", 10.0, 0.942, 9.4),
        ],
    ),
    (
        "BF0000000004",
        0.55,
        73.4,
        "Highly similar",
        4,
        Decimal("2.50"),
        [
            ("living_area", "Living Area", 24.0, 0.914, 21.9),
            ("bedrooms", "Bedrooms", 14.0, None, None),
            ("bathrooms", "Bathrooms", 12.0, None, None),
            ("quality", "Quality", 16.0, 1.0, 16.0),
            ("age", "Age", 8.0, 0.76, 6.1),
            ("stories", "Stories", 4.0, 0.35, 1.4),
            ("building_character", "Building Character", 4.0, None, None),
            ("land_size", "Land Size", 10.0, 0.54, 5.4),
            ("features", "Features", 4.0, 0.0, 0.0),
            ("distance", "Distance", 4.0, 0.961, 3.8),
        ],
    ),
    (
        "BF0000000002",
        0.28,
        52.5,
        "Good match",
        None,
        None,
        [
            ("land_size", "Land Size", 80.0, 0.471, 37.7),
            ("features", "Features", 10.0, 0.5, 5.0),
            ("distance", "Distance", 10.0, 0.981, 9.8),
        ],
    ),
]


class HarrisSimilarityGoldenTests(TestCase):
    def test_residential_search_ranks_filters_and_breaks_ties_as_captured(self):
        subject = build_harris_residential_scenario()

        results = find_similar_harris(subject)

        self.assertEqual(ranked_search(results, "account_number"), HR_SEARCH)

    def test_residential_comparables_lookup_matches_captured(self):
        subject = build_harris_residential_scenario()

        comps = harris_adapter.find_comps(subject, **LOOKUP)

        self.assertEqual(ranked_comps(comps), HR_COMPS)

    def test_building_less_subject_search_matches_captured(self):
        subject = build_harris_land_subject_scenario()

        results = find_similar_harris(subject)

        self.assertEqual(ranked_search(results, "account_number"), HL_SEARCH)

    def test_building_less_subject_comparables_lookup_matches_captured(self):
        subject = build_harris_land_subject_scenario()

        comps = harris_adapter.find_comps(subject, **LOOKUP)

        self.assertEqual(ranked_comps(comps), HL_COMPS)


class BrazosSimilarityGoldenTests(TestCase):
    def test_residential_search_ranks_and_filters_as_captured(self):
        subject = build_brazos_residential_scenario()

        results = find_similar_brazos(subject, tax_year=BRAZOS_TAX_YEAR)

        self.assertEqual(ranked_search(results, "prop_id"), BR_SEARCH)

    def test_residential_comparables_lookup_drops_ineligible_candidates_as_captured(self):
        subject = build_brazos_residential_scenario()

        comps = brazos_adapter.find_comps(subject, **LOOKUP)

        self.assertEqual(ranked_comps(comps), BR_COMPS_50)

    def test_comparables_lookup_truncates_before_dropping_ineligible_candidates(self):
        subject = build_brazos_residential_scenario()

        comps = brazos_adapter.find_comps(subject, **{**LOOKUP, "max_results": 2})

        self.assertEqual(ranked_comps(comps), BR_COMPS_2)

    def test_building_free_search_matches_captured(self):
        subject = build_brazos_building_free_scenario()

        results = find_similar_brazos(subject, tax_year=BRAZOS_TAX_YEAR)

        self.assertEqual(ranked_search(results, "prop_id"), BF_SEARCH)

    def test_building_free_comparables_lookup_matches_captured(self):
        subject = build_brazos_building_free_scenario()

        comps = brazos_adapter.find_comps(subject, **LOOKUP)

        self.assertEqual(ranked_comps(comps), BF_COMPS)


class BrazosScenarioImprovementIdentifierTests(TestCase):
    def test_scenarios_never_repeat_an_improvement_identifier_across_properties(self):
        build_brazos_residential_scenario()
        assert_brazos_improvement_ids_distinct()

    def test_guard_rejects_an_identifier_shared_by_two_properties(self):
        for prop_id in ("P1", "P2"):
            PropertyImprovement.objects.create(
                prop_id=prop_id, imp_id="I1", tax_year=BRAZOS_TAX_YEAR, improvement_type="R"
            )

        with self.assertRaisesMessage(AssertionError, "'I1': ['P1', 'P2']"):
            assert_brazos_improvement_ids_distinct()
