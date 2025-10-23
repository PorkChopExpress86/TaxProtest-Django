# Property Similarity Scoring Algorithm

## Overview
The similarity search algorithm finds properties comparable to a target property based on location, size, age, and features. Each similar property receives a **similarity score from 0 to 100**, where higher scores indicate more similar properties.

## How It Works

### 1. Initial Filtering
Before scoring, the algorithm filters candidate properties to ensure efficient processing:

- **Location Filter**: Only properties within the specified radius (default: 5 miles)
- **Coordinate Requirement**: Both target and candidate must have valid latitude/longitude
- **Size Filter**: If building data is available, only considers properties within 50-150% of target size
- **Active Records Only**: Only uses current/active building details and features
- **Excludes Self**: The target property is never included in results

### 2. Similarity Score Components

The similarity score is calculated based on **6 weighted factors**:

| Factor | Weight | Description |
|--------|--------|-------------|
| **Distance** | 30 points | How close the property is to the target |
| **Size Match** | 25 points | How similar the building square footage is |
| **Age Match** | 15 points | How close the year built is |
| **Feature Match** | 20 points | How many amenities (pool, garage, etc.) match |
| **Bedroom Match** | 5 points | How similar the bedroom count is |
| **Bathroom Match** | 5 points | How similar the bathroom count is |

**Total Possible Score: 100 points**

---

## Detailed Scoring Rules

### Distance (30 points max)
Closer properties score higher. Distance is calculated using the Haversine formula (great circle distance).

```
Within 1 mile:  30 points  ⭐⭐⭐
Within 2 miles: 20 points  ⭐⭐
Within 3 miles: 10 points  ⭐
Within 5 miles:  5 points
Over 5 miles:    0 points  (filtered out)
```

**Example:**
- Target at (29.7648, -95.3605)
- Candidate at (29.7550, -95.3600) = 0.68 miles away
- **Score: 30 points**

---

### Size Match (25 points max)
Based on heated area (living space square footage). Penalizes large size differences.

```
Within 10% of target:  25 points  ⭐⭐⭐⭐⭐
Within 20% of target:  20 points  ⭐⭐⭐⭐
Within 30% of target:  10 points  ⭐⭐
Within 50% of target:   5 points  ⭐
Over 50% different:     0 points
```

**Calculation:**
```
diff_percent = |target_sqft - candidate_sqft| / target_sqft
```

**Example:**
- Target: 2,000 sq ft
- Candidate: 2,200 sq ft
- Difference: 200 / 2,000 = 10%
- **Score: 25 points** (within 10%)

**Size Tolerance Examples:**
| Target | 10% (25pts) | 20% (20pts) | 30% (10pts) | 50% (5pts) |
|--------|-------------|-------------|-------------|------------|
| 1,500 | 1,350-1,650 | 1,200-1,800 | 1,050-1,950 | 750-2,250 |
| 2,000 | 1,800-2,200 | 1,600-2,400 | 1,400-2,600 | 1,000-3,000 |
| 3,000 | 2,700-3,300 | 2,400-3,600 | 2,100-3,900 | 1,500-4,500 |

---

### Age Match (15 points max)
Based on year built. Properties from the same era score higher.

```
Within  2 years: 15 points  ⭐⭐⭐
Within  5 years: 12 points  ⭐⭐
Within 10 years:  8 points  ⭐
Within 15 years:  4 points
Over 15 years:    0 points
```

**Example:**
- Target: Built 2005
- Candidate: Built 2007
- Difference: 2 years
- **Score: 15 points**

---

### Feature Match (20 points max)
Compares amenities like pools, garages, patios, etc. Uses [Jaccard similarity](https://en.wikipedia.org/wiki/Jaccard_index).

```
Score = (matching_features / total_unique_features) × 20
```

**Jaccard Similarity Formula:**
```
similarity = |A ∩ B| / |A ∪ B|

Where:
  A ∩ B = features in both properties
  A ∪ B = all unique features across both properties
```

**Common Feature Codes:**
- `POOL` - Swimming Pool
- `SPA` - Spa/Hot Tub
- `DETGAR` - Detached Garage
- `CARPORT` - Carport
- `PATIO` - Covered Patio
- `SPRNK` - Sprinkler System
- `FENCE` - Fence
- `GAZEBO` - Gazebo
- `TENNCT` - Tennis Court
- `POOLHTR` - Pool Heater

**Examples:**

**Example 1: Identical Features**
```
Target:    [POOL, SPA, DETGAR]
Candidate: [POOL, SPA, DETGAR]

Intersection: 3 (all match)
Union:        3 (total unique)
Similarity:   3/3 = 100%
Score:        20 points ⭐⭐⭐⭐⭐
```

**Example 2: Partial Match**
```
Target:    [POOL, SPA, DETGAR, PATIO]
Candidate: [POOL, DETGAR, FENCE]

Intersection: 2 (POOL, DETGAR)
Union:        5 (POOL, SPA, DETGAR, PATIO, FENCE)
Similarity:   2/5 = 40%
Score:        8 points ⭐⭐
```

**Example 3: No Overlap**
```
Target:    [POOL, SPA]
Candidate: [FENCE, SPRNK]

Intersection: 0 (no match)
Union:        4 (all different)
Similarity:   0/4 = 0%
Score:        0 points
```

---

### Bedroom Match (5 points max)
Simple comparison of bedroom counts.

```
Exact match:        5 points  ⭐
Off by 1 bedroom:   3 points
Off by 2+ bedrooms: 0 points
```

**Example:**
- Target: 3 bedrooms
- Candidate: 3 bedrooms
- **Score: 5 points**

---

### Bathroom Match (5 points max)
Allows for half-bath differences.

```
Within 0.5 bathrooms: 5 points  ⭐
Within 1.0 bathrooms: 3 points
Over 1.0 difference:  0 points
```

**Example:**
- Target: 2.5 bathrooms
- Candidate: 2.0 bathrooms
- Difference: 0.5
- **Score: 5 points**

---

## Complete Scoring Example

### Target Property
```
Address:   123 Main St, Houston, TX 77002
Location:  29.7648, -95.3605
Size:      2,000 sq ft
Built:     2005
Bedrooms:  3
Bathrooms: 2.5
Features:  Pool, Spa, Detached Garage
```

### Candidate Property #1 (Excellent Match)
```
Address:   456 Oak Ave, Houston, TX 77002
Location:  29.7650, -95.3610
Distance:  0.3 miles
Size:      2,100 sq ft (5% diff)
Built:     2006 (1 year diff)
Bedrooms:  3 (exact match)
Bathrooms: 2.5 (exact match)
Features:  Pool, Spa, Detached Garage (all match)
```

**Score Breakdown:**
```
Distance:   30 pts  (< 1 mile)
Size:       25 pts  (5% difference, within 10%)
Age:        15 pts  (1 year difference, within 2)
Features:   20 pts  (3/3 match = 100%)
Bedrooms:    5 pts  (exact match)
Bathrooms:   5 pts  (exact match)
─────────────────
TOTAL:     100 pts ⭐⭐⭐⭐⭐ (Perfect match!)
```

### Candidate Property #2 (Good Match)
```
Address:   789 Elm St, Houston, TX 77003
Location:  29.7500, -95.3550
Distance:  1.2 miles
Size:      2,300 sq ft (15% diff)
Built:     2008 (3 years diff)
Bedrooms:  4 (1 more)
Bathrooms: 2.0 (0.5 less)
Features:  Pool, Detached Garage (2 of 3 match)
```

**Score Breakdown:**
```
Distance:   20 pts  (1-2 miles)
Size:       20 pts  (15% difference, within 20%)
Age:        12 pts  (3 years difference, within 5)
Features:   13 pts  (2/4 features = 50% similarity)
Bedrooms:    3 pts  (off by 1)
Bathrooms:   5 pts  (0.5 difference)
─────────────────
TOTAL:      73 pts ⭐⭐⭐⭐ (Good match)
```

### Candidate Property #3 (Fair Match)
```
Address:   321 Pine Rd, Houston, TX 77004
Location:  29.7400, -95.3500
Distance:  2.5 miles
Size:      2,600 sq ft (30% diff)
Built:     1998 (7 years diff)
Bedrooms:  4 (1 more)
Bathrooms: 3.0 (0.5 more)
Features:  Pool, Fence, Patio (1 of 3 match)
```

**Score Breakdown:**
```
Distance:   10 pts  (2-3 miles)
Size:       10 pts  (30% difference, within 30%)
Age:         8 pts  (7 years difference, within 10)
Features:    7 pts  (1/5 features = 20% similarity)
Bedrooms:    3 pts  (off by 1)
Bathrooms:   5 pts  (0.5 difference)
─────────────────
TOTAL:      43 pts ⭐⭐ (Fair match)
```

---

## Understanding the Results

### Score Ranges
```
90-100: Exceptional match  ⭐⭐⭐⭐⭐ (Very rare, nearly identical)
70-89:  Excellent match    ⭐⭐⭐⭐   (Highly comparable)
50-69:  Good match         ⭐⭐⭐     (Reasonably comparable)
30-49:  Fair match         ⭐⭐       (Somewhat similar)
0-29:   Poor match         ⭐         (Not very comparable)
```

### Default Filters
By default, the search only returns properties with:
- **Minimum score:** 30 points (Fair match or better)
- **Maximum distance:** 5 miles
- **Maximum results:** 50 properties (sorted by score)

These can be adjusted via URL parameters:
```
/similar/0123456789012/?max_distance=3&min_score=50&max_results=20
```

---

## Use Cases

### Tax Protest / Appeals
Properties with scores **70+** are typically strong comparables for:
- Property tax protests
- Appraisal appeals
- Market value analysis

### Market Research
Properties with scores **50+** provide:
- Neighborhood market trends
- Price per square foot comparisons
- Feature value analysis

### General Interest
Properties with scores **30+** show:
- Nearby properties
- Similar home styles in area
- Neighborhood characteristics

---

## Technical Implementation

### Distance Calculation
Uses the **Haversine formula** to calculate great circle distance between two points on Earth:

```python
def haversine_distance(lat1, lon1, lat2, lon2):
    # Convert to radians
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    
    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    
    # Earth radius in miles
    return 3959 * c
```

### Performance Optimizations
1. **Bounding box pre-filter**: Only evaluates properties within a rough square around the target
2. **Database prefetch**: Loads related building and feature data efficiently
3. **Candidate limit**: Processes maximum 500 candidates before scoring
4. **Result limit**: Returns only top N results (default: 50)

### Data Requirements
For accurate scoring, properties should have:
- ✅ Latitude/Longitude (required)
- ✅ Building details (size, year, beds, baths)
- ✅ Extra features (pool, garage, etc.)

Properties missing data receive partial scores based on available information.

---

## API Reference

### Function: `find_similar_properties()`

```python
from data.similarity import find_similar_properties

results = find_similar_properties(
    account_number='0123456789012',
    max_distance_miles=5.0,      # Search radius
    max_results=50,              # Number of results
    min_score=30.0              # Minimum similarity score
)
```

### Returns
```python
[
    {
        'property': PropertyRecord,           # Django model instance
        'building': BuildingDetail,           # Building data (or None)
        'features': [ExtraFeature, ...],     # List of features
        'distance': 1.23,                     # Miles from target
        'similarity_score': 85.5,            # Calculated score
    },
    ...
]
```

### Web Endpoint
```
GET /similar/<account_number>/
GET /similar/<account_number>/?max_distance=3&min_score=50&max_results=20
```

---

## Future Enhancements

Potential improvements to the algorithm:

1. **Market Value Factor**: Compare assessed values (±20% tolerance)
2. **Neighborhood Preference**: Bonus points for same ZIP code or subdivision
3. **Lot Size Factor**: Compare land area for properties with large lots
4. **Building Type**: Prefer same building style (ranch, two-story, etc.)
5. **Recent Sales**: Prioritize properties with recent sale dates
6. **Custom Weights**: Allow users to adjust factor weights
7. **Machine Learning**: Train model on actual comparable sales data

---

## See Also
- [GIS.md](../GIS.md) - Location data and coordinate handling
- [DATABASE.md](../DATABASE.md) - Data sources and import process
- [data/similarity.py](../data/similarity.py) - Source code implementation
