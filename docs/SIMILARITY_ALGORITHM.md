# Property Similarity Search Algorithm Documentation

## Overview

The similarity search algorithm finds properties comparable to a target property based on multiple weighted criteria. This is useful for property tax appeals, comparative market analysis, and identifying similar real estate.

**Last Updated:** November 24, 2025  
**Default Search Radius:** 10 miles  
**Maximum Score:** 100 points

---

## Search Parameters

### Core Configuration

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `max_distance_miles` | 10.0 | 1-20 | Maximum search radius from target property (miles) |
| `max_results` | 50 | 1-200 | Maximum number of similar properties to return |
| `min_score` | 30.0 | 0-100 | Minimum similarity score to include in results |

### Size Filtering (Pre-screening)

Before detailed scoring, candidates are filtered by size:
- **Minimum Area:** 50% of target property size
- **Maximum Area:** 150% of target property size
- Only applies if target has building data

---

## Scoring Criteria & Weights

The algorithm assigns up to **100 points** based on seven weighted criteria (distance filtered only). Higher scores indicate more similar properties.

**Important:** Distance is used to filter candidates (within the search radius) but does NOT affect the similarity score. This allows you to compare properties with similar attributes and see how their price/sqft compares, regardless of whether they're 1 mile or 10 miles away from the target.

### 1. Heated Size Match (22 points max) 📏

**Weight:** 22% of total score  
**Importance:** HIGH - Heated area is a primary physical characteristic (bed/bath also elevated)

| Size Difference | Points | Example (2000 sq ft target) |
|----------------|--------|----------------------------|
| Within 10% | 25 | 1,800 - 2,200 sq ft |
| Within 20% | 20 | 1,600 - 2,400 sq ft |
| Within 30% | 13 | 1,400 - 2,600 sq ft |
| Within 50% | 7 | 1,000 - 3,000 sq ft |
| Over 50% | 0 | Not comparable |

**Field Used:** `BuildingDetail.heat_area` (heated/conditioned square footage)

**Why Heated Area?** More consistent than total area; excludes garages, porches, unfinished spaces that vary widely in value contribution.

---

### 2. Lot Size Match (15 points max) 🌳

**Weight:** 15% of total score  
**Importance:** HIGH - Land value and parcel utility materially affect comparability

| Lot Size Difference | Points | Example (8,000 sq ft target) |
|--------------------|--------|-----------------------------|
| Within 10% | 15 | 7,200 - 8,800 sq ft |
| Within 20% | 12 | 6,400 - 9,600 sq ft |
| Within 30% | 8 | 5,600 - 10,400 sq ft |
| Within 50% | 4 | 4,000 - 12,000 sq ft |
| Over 50% | 0 | Not comparable |

**Field Used:** `PropertyRecord.land_area`

**Rationale:** Similar lot sizes help ensure external utility (yard, setback, expansion potential) is comparable.

---

### 3. Age Match (5 points max) 📅

**Weight:** 5% of total score  
**Importance:** LOW - De-emphasized versus size and core livability metrics

| Age Difference | Points | Note |
|---------------|--------|------|
| ≤ 2 years | 5 | Essentially same era |
| ≤ 5 years | 4 | Similar construction period |
| ≤ 10 years | 3 | Same general vintage |
| ≤ 15 years | 2 | Different construction standards |
| > 15 years | 0 | Different building codes/styles |

**Field Used:** `BuildingDetail.year_built`

**Rationale:** Properties built in the same era share construction methods, materials, code requirements, and depreciation schedules.

---

### 4. Quality Match (12 points max) ⭐

**Weight:** 15% of total score  
**Importance:** MEDIUM-HIGH - Quality grade indicates finish level and market segment

**HCAD Quality Codes:**
- **X** = Superior (rank 7)
- **A** = Excellent (rank 6)
- **B** = Good (rank 5)
- **C** = Average (rank 4)
- **D** = Low (rank 3)
- **E** = Very Low (rank 2)
- **F** = Poor (rank 1)

| Quality Match | Points | Note |
|--------------|--------|------|
| Exact match | 12 | Same quality tier |
| 1 level off | 8 | Similar quality (e.g., B vs A) |
| 2 levels off | 4 | Noticeable difference |
| 3+ levels off | 0 | Different market segment |

**Field Used:** `BuildingDetail.quality_code`

**Why Quality Matters:** Differentiates custom homes from builder-grade, luxury from economy finishes.

---

### 5. Bedroom Match (18 points max) 🛏️

**Weight:** 20% of total score  
**Importance:** HIGH - Bedroom count is a primary search criterion

| Bedroom Difference | Points | Example (4BR target) |
|-------------------|--------|---------------------|
| Exact match | 18 | 4 bedrooms |
| Off by 1 | 10 | 3 or 5 bedrooms |
| Off by 2 | 5 | 2 or 6 bedrooms |
| Off by 3+ | 0 | Not comparable |

**Field Used:** `BuildingDetail.bedrooms`

**Source:** Extracted from HCAD fixtures file (RMB room code)

---

### 6. Bathroom Match (18 points max) 🚿

**Weight:** 20% of total score  
**Importance:** HIGH - Bathroom count indicates functionality and luxury level

| Bathroom Difference | Points | Example (2.5 bath target) |
|--------------------|--------|--------------------------|
| ≤ 0.5 difference | 18 | 2.0 or 3.0 baths |
| ≤ 1.0 difference | 11 | 1.5 or 3.5 baths |
| ≤ 1.5 difference | 5 | 1.0 or 4.0 baths |
| > 1.5 difference | 0 | Not comparable |

**Field Used:** `BuildingDetail.bathrooms` (supports half-baths)

**Source:** Extracted from HCAD fixtures file (RMF/RMH room codes)

---

### 7. Feature Match (10 points max) 🏊

**Weight:** 10% of total score  
**Importance:** MEDIUM - Amenities indicate property tier and buyer appeal

**Features Compared:**
- Pools (in-ground, above-ground)
- Garages (attached, detached, carport)
- Outdoor structures (patios, decks, gazebos)
- Special improvements (workshops, guest houses)

**Scoring Method:** Jaccard Similarity Coefficient

```
Score = (Matching Features / Total Unique Features) × 10
```

**Example:**
- Target has: `POOL, DETGAR, PATIO`
- Candidate has: `POOL, DETGAR, DECK`
- Intersection: 2 (POOL, DETGAR)
- Union: 4 (POOL, DETGAR, PATIO, DECK)
- Score: (2/4) × 10 = 5 points

**Fields Used:** `ExtraFeature.feature_code` for target and candidate (only active records)

---

## Score Interpretation

| Score Range | Interpretation | Use Case |
|------------|---------------|----------|
| 90-100 | Excellent match | Strong comparable for appeals |
| 70-89 | Good match | Acceptable comparable |
| 50-69 | Moderate match | Borderline comparable |
| 30-49 | Weak match | Background context only |
| 0-29 | Poor match | Not useful (filtered out by default) |

---

## Algorithm Workflow

### Step 1: Target Property Validation
1. Fetch target property by account number
2. Verify lat/long coordinates exist
3. Load active building details
4. Load active extra features

**Exit early if:** Target has no coordinates (can't calculate distance)

### Step 2: Bounding Box Calculation
Calculate rough search box to limit database queries:

```python
lat_range = max_distance_miles / 69.0  # 1° latitude ≈ 69 miles
lon_range = max_distance_miles / (69.0 * cos(latitude))  # Adjust for longitude
```

### Step 3: Candidate Pre-filtering
Query properties matching ALL criteria:
- Within bounding box (lat/long range)
- Has coordinates (not null)
- Heated area between 50-150% of target (if target has building data)
- Exclude target property itself
- Use distinct() to avoid duplicates

**Performance:** Limits to 500 candidates to prevent excessive processing

### Step 4: Distance & Score Calculation
For each candidate:
1. Calculate precise Haversine distance
2. Skip if > `max_distance_miles`
3. Load active building & features
4. Calculate 7-part similarity score (distance filtered only)
5. Skip if score < `min_score`

### Step 5: Ranking & Return
1. Sort by similarity score (descending)
2. Return top `max_results` properties
3. Include distance and score in results

---

## Technical Implementation

### Function Signature

```python
def find_similar_properties(
    account_number: str,
    max_distance_miles: float = 10.0,
    max_results: int = 50,
    min_score: float = 30.0,
) -> List[Dict]:
```

### Return Format

```python
[
    {
        'property': PropertyRecord,          # Main property record
        'building': BuildingDetail,          # Active building details
        'features': List[ExtraFeature],      # Active extra features
        'distance': 2.34,                    # Miles from target
        'similarity_score': 78.5,            # 0-100 score
    },
    # ... more results
]
```

### Performance Considerations

1. **Bounding Box Pre-filter:** Reduces candidates by ~98% before expensive calculations
2. **Size Pre-filter:** Eliminates obviously mismatched properties
3. **500 Candidate Limit:** Prevents runaway queries in dense areas
4. **Active-Only Records:** Uses `is_active=True` to skip old/replaced data
5. **Prefetch Related:** Loads buildings & features efficiently

---

## Adjustment Guidelines

### To Prioritize Location More
```python
# Increase distance weight, decrease others proportionally
Distance: 30 points (from 25)
Size: 18 points (from 20)
Age: 8 points (from 10)
```

### To Prioritize Size More
```python
# Increase size weight, tighten tolerances
Size: 25 points (from 20)
- Within 10%: 25 pts
- Within 15%: 18 pts
- Within 20%: 10 pts
```

### To Require More Features
```python
# Increase feature weight, require higher threshold
Features: 20 points (from 15)
min_score: 40.0 (from 30.0)
```

### To Loosen Age Requirements
```python
# Extend age tolerance bands
- ≤ 5 years: 10 pts
- ≤ 10 years: 8 pts
- ≤ 20 years: 5 pts
- ≤ 30 years: 3 pts
```

---

## Example Use Cases

### Property Tax Appeal
```python
# Find strong matches for appeal evidence
results = find_similar_properties(
    account_number="0123456789",
    max_distance_miles=3.0,      # Same neighborhood
    max_results=10,              # Top 10 only
    min_score=60.0,              # Good matches only
)
```

### Market Analysis
```python
# Broad search for market trends
results = find_similar_properties(
    account_number="0123456789",
    max_distance_miles=10.0,     # Wide area
    max_results=100,             # Many examples
    min_score=40.0,              # Include moderate matches
)
```

### Comparative Pricing
```python
# Find near-identical properties for pricing
results = find_similar_properties(
    account_number="0123456789",
    max_distance_miles=5.0,
    max_results=20,
    min_score=70.0,              # Strong matches only
)
```

---

## Data Sources

| Criterion | HCAD File | Field(s) | Import Command |
|-----------|-----------|----------|----------------|
| Location | `Parcels/Gis/pdata/ParcelsCity/ParcelsCity.shp` | `latitude`, `longitude` | `load_gis_data` |
| Size | `Real_building_land/building_res.txt` | `heat_area` | `import_building_data` |
| Age | `Real_building_land/building_res.txt` | `year_built` | `import_building_data` |
| Quality | `Real_building_land/building_res.txt` | `quality_code` | `import_building_data` |
| Bedrooms | `Real_building_land/fixtures.txt` | Room code `RMB` | `import_building_data` |
| Bathrooms | `Real_building_land/fixtures.txt` | Room codes `RMF`, `RMH` | `import_building_data` |
| Features | `Real_building_land/extra_features.txt` | `feature_code` | `import_building_data` |

---

## Known Limitations

1. **No Story Count:** HCAD data doesn't consistently include story information
2. **No Lot Size:** Parcel GIS doesn't include lot dimensions
3. **No Style:** Building style (ranch, colonial, etc.) not in dataset
4. **No Condition:** Beyond quality code, no detailed condition assessment
5. **No Interior Photos:** Cannot compare finishes, layouts, or aesthetics
6. **Commercial Properties:** Algorithm tuned for residential; may not work well for commercial
7. **Rural Properties:** Few comparables in low-density areas may return poor matches

---

## Future Enhancements

### Potential Additions
- **School District Matching:** Add points for same ISD
- **HOA Presence:** Match properties with/without HOA
- **Lot Size Ratio:** If parcel area data becomes available
- **Price Per Square Foot:** Include market value similarity
- **Sale Date Recency:** Prioritize recently sold comparables
- **Subdivision Name:** Exact neighborhood matching
- **Story Count:** When/if data becomes available

### Algorithm Improvements
- **Machine Learning:** Train on appraiser selections to optimize weights
- **Market Segments:** Different weights for luxury vs. economy properties
- **Time-based Weighting:** Depreciate older sales data
- **Cluster Analysis:** Identify micro-markets for better local comparisons

---

## Testing & Validation

### Unit Tests
Located in: `data/tests/test_similarity.py` (if exists)

### Manual Testing
```python
# In Django shell
from data.similarity import find_similar_properties

# Test with known property
results = find_similar_properties("0123456789", max_distance_miles=10)

# Inspect top match
top = results[0]
print(f"Score: {top['similarity_score']}")
print(f"Distance: {top['distance']} miles")
print(f"Address: {top['property'].situs_street}")
```

### Visual Testing
1. Search for your own property address
2. Review "Similar Properties" results
3. Verify matches are reasonable
4. Check that sorting by price/sqft works correctly

---

## Related Documentation

- **[SIMILARITY_SCORING.md](SIMILARITY_SCORING.md)** - Detailed scoring examples
- **[SIMILARITY_QUICKREF.md](SIMILARITY_QUICKREF.md)** - Quick reference guide
- **[GIS.md](../GIS.md)** - Location data and coordinate imports
- **[DATABASE.md](../DATABASE.md)** - Data model and import processes

---

## Changelog

### November 24, 2025
- **Changed:** Increased default search radius from 7 miles to 10 miles
- **Added:** Created comprehensive algorithm documentation
- **Note:** Distance scoring still caps at 5 miles (>5 = 0 points) to maintain local focus

### October 2025
- Initial implementation with 7-criterion scoring system
- Added bedroom/bathroom matching from fixtures data
- Implemented PPSF sorting in UI

---

## Contact & Support

For questions about this algorithm or to request adjustments:
1. Review this documentation first
2. Test proposed changes in Django shell
3. Update scoring weights in `data/similarity.py`
4. Run regression tests before deploying
5. Document changes in this file's Changelog
