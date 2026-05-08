# Quick Reference: Similarity Scoring

## Current Weights

| Factor | Points |
| --- | ---: |
| Living area | 24 |
| Bedrooms | 14 |
| Bathrooms | 12 |
| Land size | 10 |
| Quality | 10 |
| Age | 8 |
| Condition | 6 |
| Stories | 4 |
| Building type/style/class | 4 |
| Extra features | 4 |
| Distance | 4 |

Total: 100 points.

## Labels

| Score | Label |
| --- | --- |
| `84.0-100.0` | Best match |
| `70.0-83.9` | Highly similar |
| `52.0-69.9` | Good match |
| `36.0-51.9` | OK match |
| `0.0-35.9` | Broad match |

## What Changed

- Scores are displayed with one decimal place.
- High-90 scores require near-identical major attributes.
- Living area, lot size, bedrooms, bathrooms, and age now separate near ties more aggressively.
- Distance contributes lightly instead of being only a pre-filter.
- Similar Properties, Evidence Report, CSV, and PDF outputs expose score details.

## Python Usage

```python
from data.similarity import calculate_similarity_details, find_similar_properties

results = find_similar_properties(
    account_number="0123456789012",
    max_distance_miles=10.0,
    max_results=50,
    min_score=70.0,
)

for result in results:
    print(result["similarity_score"], result["property"].address)
    for component in result["score_breakdown"]:
        if component["available"]:
            print(component["label"], component["points"], "/", component["weight"])
```

Use `calculate_similarity_details()` when comparing two known properties directly and you need both the final score and component breakdown.
