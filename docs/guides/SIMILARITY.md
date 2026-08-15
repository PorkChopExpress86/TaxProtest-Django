# Property Similarity Scoring & Algorithm Guide

The similarity scoring engine identifies and ranks physically comparable properties for property tax protest research and appraisal evidence packages.

It is implemented as a two-tier architecture:
1. **Shared Pure-Math Core** ([`counties/common/similarity_math.py`](file:///home/specter/dev/TaxProtest-Django/counties/common/similarity_math.py)): Curve interpolation, distance calculation, Jaccard feature similarity, normalized score breakdowns, and rating tiers.
2. **County Adapters & Scoring Modules** ([`counties/harris/similarity.py`](file:///home/specter/dev/TaxProtest-Django/counties/harris/similarity.py), [`counties/brazos/similarity.py`](file:///home/specter/dev/TaxProtest-Django/counties/brazos/similarity.py)): County-specific model attribute mappings, code definitions, and candidate database queries.

---

## 1. Search & Filtering Workflow

When searching for comparable properties:

1. **Target Lookup**: Fetch the subject property by account number and verify valid geographic coordinates (`latitude`, `longitude`).
2. **Radius Bounding Box**: Construct a bounding box in degrees from `max_distance_miles` (default: 10 miles).
3. **Database Pre-Filtering**:
   - Filter candidates within the bounding box.
   - For improved residential parcels with heated area, filter candidates to 50%–150% of the target living area.
   - For Harris County, require active `BuildingDetail` records (`is_active=True`) and data-ready flags where applicable.
4. **Candidate Distance Calculation**: Calculate exact Haversine distance in miles from the subject property.
5. **Candidate Scoring**: Compute granular component scores (`0.0` to `100.0`) in Python for up to 2,000 candidate records.
6. **Sorting & Selection**: Sort descending by `similarity_score`, then ascending by `distance`, then by account number.

---

## 2. Residential Score Components & Weights

Residential properties are scored on a 100-point scale across 11 physical factors:

| Component | Max Points | Measurement Method | Data Source (Harris / Brazos) |
|---|---:|---|---|
| **Living Area** | 24 | Piecewise linear curve on percent difference in heated square footage. | `BuildingDetail.heat_area` / `PropertyImprovement.living_area` |
| **Bedrooms** | 14 | Exact match = full credit; 1-room diff = partial; 2+ rooms fall quickly. | `BuildingDetail.bedrooms` / `PropertyBuildingCharacteristic.bedrooms` |
| **Bathrooms** | 12 | Exact match = full credit; half-bath diffs scored closer than full baths. | `BuildingDetail.bathrooms` / `PropertyBuildingCharacteristic.full_bath + half*0.5` |
| **Land Size** | 10 | Piecewise linear curve on percent difference in parcel square footage / acreage. | `PropertyRecord.land_area` / `PropertyAccount.land_acres` |
| **Quality** | 10 | Ranked code comparison: `X=7, A=6, B=5, C=4, D=3, E=2, F=1` (or Brazos numeric quality digits). | `BuildingDetail.quality_code` / `PropertyImprovement.quality` |
| **Age** | 8 | Effective year > Remodel year > Year built difference tolerance. | `eff_year` or `year_built` |
| **Condition** | 6 | Ranked or categorical condition code comparison. | `BuildingDetail.condition_code` / `PropertyImprovement.condition` |
| **Stories** | 4 | Absolute story count difference. | `BuildingDetail.stories` / `PropertyBuildingCharacteristic.has_second_floor` |
| **Building Type / Style** | 4 | Categorical matching on style, structural type, or class code. | `building_style_code` / `PropertyImprovement.state_class` |
| **Extra Features** | 4 | Jaccard similarity: $\frac{|A \cap B|}{|A \cup B|}$ on active amenity codes (pools, detached garages, sheds, porches). | `ExtraFeature.feature_code` / `PropertyExtraFeature.feature_code` |
| **Distance** | 4 | Distance relative to maximum radius; breaks ties among physically similar comps. | Calculated Haversine distance |

**Total:** 100 points possible.

---

## 3. Land-Only Scoring

Properties without primary improvements are evaluated using a land-dedicated weight model:

| Component | Weight | Matching Method |
|---|---:|---|
| **Land Area** | 80% | Percent difference in parcel size. |
| **Extra Features** | 10% | Jaccard similarity on site improvements. |
| **Distance** | 10% | Distance relative to search radius. |

---

## 4. Score Calculation Formula & Completeness

Each factor produces a normalized similarity value $s_i \in [0.0, 1.0]$:

$$\text{factor\_points}_i = w_i \times s_i$$

$$\text{base\_score} = \frac{\sum \text{factor\_points}_i}{\sum w_{\text{available}}}$$

$$\text{final\_score} = \text{base\_score} \times \text{completeness\_multiplier} \times 100$$

### Completeness Multiplier
- Fully populated residential records: `1.00`.
- Records with missing attributes: Scales between `0.80` and `1.00` proportional to available factor weights.
- Incomplete public records remain comparable without artificially inflating confidence compared to fully documented properties.

---

## 5. Similarity Rating Tiers & Labels

Scores are formatted with one decimal place (`94.2`) and categorized into standardized tiers:

| Score Range | Label | Interpretation for ARB Protest Evidence |
|---|---|---|
| **84.0 – 100.0** | **Best match** | Strongest physical comparables; primary evidence for unequal appraisal hearings. |
| **70.0 – 83.9** | **Highly similar** | Strong comparables with minor differences in size, age, or features. |
| **52.0 – 69.9** | **Good match** | Supportive context; review specific component breakdown. |
| **36.0 – 51.9** | **OK match** | Broad neighborhood comparison only. |
| **0.0 – 35.9** | **Broad match** | Significant physical divergence; filtered out by default threshold (`min_score=30.0`). |

---

## 6. Python API Usage

### Finding Similar Properties

```python
from counties.harris.similarity import find_similar_properties
# Or for Brazos:
# from counties.brazos.similarity import find_similar_properties

results = find_similar_properties(
    account_number="0123456789012",
    max_distance_miles=10.0,
    max_results=50,
    min_score=30.0,
)

for res in results:
    prop = res["property"]
    score = res["similarity_score"]
    dist = res["distance"]
    print(f"{score:.1f} pts | {dist:.2f} mi | {prop.address}")

    # Inspect component breakdown
    for comp in res["score_breakdown"]:
        if comp["available"]:
            print(f"  - {comp['label']}: {comp['points']:.1f}/{comp['weight']} pts (sim: {comp['similarity']:.2f})")
```

### Direct Pairwise Calculation

```python
from counties.harris.similarity import calculate_similarity_details

score, breakdown = calculate_similarity_details(
    target_prop, target_building, target_features,
    candidate_prop, candidate_building, candidate_features,
    distance=0.75,
    max_distance=10.0,
)
```

---

## 7. Evidence Reports & Export Integration

- **Similar Properties View (`/harris/similar/<account>/`, `/brazos/similar/<account>/`)**: Displays comparable cards, score badges, and interactive component breakdown drawers.
- **Protest Evidence Report (`/harris/protest/<account>/`, `/brazos/protest/<account>/`)**: Generates an equity appraisal analysis, median comparable price/sqft comparisons, and estimated tax savings.
- **CSV Export**: Includes full property attributes, distance, total score, and serialized `score_breakdown`.
- **PDF Export (`fpdf2`)**: Generates multi-page formatted ARB evidence packets containing subject property summaries, 5-year assessment histories, 10% homestead cap analyses, and top comparable evidence cards.
