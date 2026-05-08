# Property Similarity Search Algorithm

Last updated: May 8, 2026

The similarity algorithm finds physically comparable Harris County properties for tax-protest research. It filters candidates by location and broad size, then calculates a `0.0` to `100.0` score with an explainable component breakdown.

## Search Flow

1. Fetch the target `PropertyRecord` by account number.
2. Require target latitude/longitude.
3. Load the target's active building detail and active extra features.
4. Build a geographic bounding box from `max_distance_miles`.
5. If target heated area is available, pre-filter candidates to 50%-150% of target heated area.
6. Calculate precise distance in the database.
7. Score up to 2,000 nearby candidates in Python.
8. Return results sorted by score descending, then distance, then account number.

## Inputs

```python
find_similar_properties(
    account_number: str,
    max_distance_miles: float = 10.0,
    max_results: int = 50,
    min_score: float = 30.0,
)
```

The web views clamp user-supplied search parameters before calling the algorithm.

## Score Components

Residential comparisons use 100 total possible points:

| Component | Weight | Matching Method |
| --- | ---: | --- |
| Living area | 24 | Percent difference in heated/living area. |
| Bedrooms | 14 | Absolute bedroom-count difference. |
| Bathrooms | 12 | Absolute bathroom-count difference. |
| Land size | 10 | Percent difference in land area. |
| Quality | 10 | Ranked HCAD quality-code comparison. |
| Age | 8 | Effective year, then remodel year, then built year. |
| Condition | 6 | Ranked/categorical condition-code comparison. |
| Stories | 4 | Absolute story-count difference. |
| Building type | 4 | Style, type, or class categorical comparison. |
| Features | 4 | Jaccard similarity of active extra-feature codes. |
| Distance | 4 | Distance as a share of selected search radius. |

Land-only comparisons use land size, features, and distance.

## Score Calculation

Each component produces a normalized similarity from `0.0` to `1.0`.

```text
component_points = component_weight * component_similarity
base_score = sum(component_points) / sum(available_component_weights)
final_score = base_score * completeness_multiplier * 100
```

The completeness multiplier is `0.80` to `1.00` for residential comparisons. Missing HCAD attributes do not automatically make a property unusable, but incomplete records receive less confidence than fully populated records.

## Granularity Rules

The current curves are deliberately more granular than the older scoring model:

- one-bedroom and half-bath differences still receive credit, but no longer behave like near-perfect matches;
- 5%-20% living-area or lot-size differences separate otherwise similar properties;
- age differences are based on effective year when available;
- distance contributes lightly after radius filtering, which helps break ties;
- score output keeps one decimal place.

This means high-90 scores are reserved for properties that are nearly identical across the major physical traits.

## Returned Result Shape

Each result contains:

```python
{
    "property": PropertyRecord,
    "building": BuildingDetail | None,
    "features": list[ExtraFeature],
    "distance": 0.42,
    "similarity_score": 91.4,
    "score_breakdown": [
        {
            "name": "living_area",
            "label": "Living Area",
            "weight": 24.0,
            "similarity": 0.96,
            "points": 23.0,
            "available": True,
        },
        # ...
    ],
}
```

Use `calculate_similarity_score()` for the legacy numeric score API. Use `calculate_similarity_details()` when score details are needed.

## Report Integration

The Similar Properties table and Evidence Report show one-decimal scores and expandable score details. CSV export includes a `score_breakdown` column. PDF export includes the subject summary, assessment history, cap-status summary, and top comparable evidence.
