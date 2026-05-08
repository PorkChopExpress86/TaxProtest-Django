# Property Similarity Scoring

Last updated: May 8, 2026

The similarity score ranks Harris County properties from `0.0` to `100.0` based on physical comparability. Scores are shown with one decimal place so close matches can be separated more clearly; a `96.8` is meaningfully stronger than an `87.2`.

The report and CSV export include a score breakdown for each comparable. The breakdown shows each factor's label, available weight, earned points, and normalized similarity.

## Weighted Factors

| Factor | Max Points | Data Source | How It Contributes |
| --- | ---: | --- | --- |
| Living area | 24 | `BuildingDetail.heat_area` | Percent difference in heated/living area. High-90 scores require very small size differences. |
| Bedrooms | 14 | `BuildingDetail.bedrooms` | Exact match receives full credit; one-bedroom differences receive partial credit; larger gaps fall quickly. |
| Bathrooms | 12 | `BuildingDetail.bathrooms` | Exact match receives full credit; half-bath differences are treated as closer than full-bath differences. |
| Land size | 10 | `PropertyRecord.land_area` | Percent difference in parcel size. |
| Quality | 10 | `BuildingDetail.quality_code` | HCAD quality ranks: `X=7`, `A=6`, `B=5`, `C=4`, `D=3`, `E=2`, `F=1`. |
| Age | 8 | effective year, remodel year, or year built | Compares the best available effective construction year. |
| Condition | 6 | `BuildingDetail.condition_code` | Uses the same ranked-code behavior as quality where possible. |
| Stories | 4 | `BuildingDetail.stories` | Penalizes story-count differences. |
| Building type | 4 | style, type, or class | Exact building-character matches receive full credit; related codes receive partial credit. |
| Features | 4 | active `ExtraFeature.feature_code` rows | Jaccard similarity: shared feature codes divided by all unique feature codes. |
| Distance | 4 | GIS coordinates | Distance contributes lightly after candidates are filtered to the selected radius. |

Total residential weight is 100 points. Land-only comparisons use land size, features, and distance.

## Score Formula

For each available factor:

```text
factor_points = factor_weight * factor_similarity
base_score = sum(factor_points) / sum(available_factor_weights)
final_score = base_score * completeness_multiplier * 100
```

The completeness multiplier is `0.80` to `1.00` for residential properties. A property with incomplete HCAD data can still be compared, but a fully documented comparable gets more confidence.

## Why Scores Became More Granular

Earlier scoring allowed many broadly similar properties to cluster in the high 90s. The current curves are intentionally steeper:

- living-area, lot-size, bedroom, bathroom, and age differences separate close matches more aggressively;
- distance now contributes a small amount to break otherwise similar ties;
- reports show one-decimal scores rather than rounded whole numbers;
- each comparable exposes score details so users can see why it ranked where it did.

The goal is not to punish useful comparables. The goal is to reserve scores above roughly `95` for properties that are nearly identical across the major physical attributes.

## Score Labels

| Range | Label | Meaning |
| --- | --- | --- |
| `84.0-100.0` | Best match | Strongest physical comparables. |
| `70.0-83.9` | Highly similar | Useful comparables with some visible differences. |
| `52.0-69.9` | Good match | Supportive context; review score details before relying on it. |
| `36.0-51.9` | OK match | Broad comparison only. |
| `0.0-35.9` | Broad match | Usually too different for primary evidence. |

## Report Outputs

The Similar Properties page and Evidence Report both display one-decimal scores and expandable score details. The Evidence Report CSV includes a `score_breakdown` column. The PDF export includes the subject summary, assessment history, cap status summary, and top comparable evidence.
