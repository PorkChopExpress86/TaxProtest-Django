# Quick Reference: Property Similarity Scoring

## Score Weights (Total: 100 points)

```
┌─────────────────────────────────────────────────────────┐
│  DISTANCE (30 pts)                                      │
│  ════════════════════════════════════════════════════   │
│  < 1 mile:  ██████████████████████████████   30 pts    │
│  1-2 miles: ████████████████████            20 pts     │
│  2-3 miles: ██████████                      10 pts     │
│  3-5 miles: ███                              5 pts     │
│  > 5 miles:                                  0 pts     │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  SIZE MATCH (25 pts)                                    │
│  ════════════════════════════════════════════════       │
│  ±10% sqft: █████████████████████████     25 pts       │
│  ±20% sqft: ████████████████████          20 pts       │
│  ±30% sqft: ██████████                    10 pts       │
│  ±50% sqft: ███                            5 pts       │
│  >50% diff:                                0 pts       │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  AGE MATCH (15 pts)                                     │
│  ════════════════                                       │
│  ±2 years:  ███████████████               15 pts       │
│  ±5 years:  ████████████                  12 pts       │
│  ±10 years: ████████                       8 pts       │
│  ±15 years: ████                           4 pts       │
│  >15 years:                                0 pts       │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  FEATURES (20 pts) - Jaccard Similarity                 │
│  ═══════════════════                                    │
│  100% match: ████████████████████          20 pts      │
│   50% match: ██████████                    10 pts      │
│    0% match:                                0 pts      │
│                                                         │
│  Common: POOL, SPA, DETGAR, PATIO, FENCE, SPRNK        │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  BEDROOMS (5 pts)                                       │
│  ═════                                                  │
│  Exact:     █████                           5 pts      │
│  ±1 room:   ███                             3 pts      │
│  ±2+ rooms:                                 0 pts      │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  BATHROOMS (5 pts)                                      │
│  ═════                                                  │
│  ±0.5:      █████                           5 pts      │
│  ±1.0:      ███                             3 pts      │
│  >1.0 diff:                                 0 pts      │
└─────────────────────────────────────────────────────────┘
```

## Score Interpretation

| Range  | Grade | Meaning | Use Case |
|--------|-------|---------|----------|
| 90-100 | A+ ⭐⭐⭐⭐⭐ | Exceptional match | Primary comparable for tax appeals |
| 70-89  | A  ⭐⭐⭐⭐ | Excellent match | Strong comparable for protests |
| 50-69  | B  ⭐⭐⭐ | Good match | Valid comparable, market research |
| 30-49  | C  ⭐⭐ | Fair match | General neighborhood comparison |
| 0-29   | D  ⭐ | Poor match | Not recommended for comparables |

## Usage Examples

### Web Interface
```
Navigate to similar properties from search results:
→ Click "Find Similar" button next to any property
→ View results sorted by similarity score (highest first)
```

### URL Parameters
```
/similar/<account_number>/
/similar/<account_number>/?max_distance=3
/similar/<account_number>/?max_distance=3&min_score=50&max_results=20
```

**Parameters:**
- `max_distance` - Search radius in miles (default: 5)
- `min_score` - Minimum similarity score (default: 30)
- `max_results` - Maximum results to return (default: 50)

### API Usage (Python)
```python
from data.similarity import find_similar_properties

# Find similar properties
results = find_similar_properties(
    account_number='0123456789012',
    max_distance_miles=5.0,
    max_results=50,
    min_score=30.0
)

# Process results
for match in results:
    prop = match['property']
    score = match['similarity_score']
    distance = match['distance']
    
    print(f"{prop.address} - {score} pts ({distance} mi)")
```

## Example: Perfect Match (100 points)

```
Target Property:
  123 Main St
  2,000 sqft, Built 2005, 3 bed, 2.5 bath
  Features: Pool, Spa, Detached Garage
  Location: 29.7648, -95.3605

Similar Property:
  456 Oak Ave (0.5 miles away)
  2,100 sqft, Built 2006, 3 bed, 2.5 bath
  Features: Pool, Spa, Detached Garage
  Location: 29.7650, -95.3610

Score Breakdown:
  Distance:   30 pts  (within 1 mile)
  Size:       25 pts  (5% difference)
  Age:        15 pts  (1 year difference)
  Features:   20 pts  (all features match)
  Bedrooms:    5 pts  (exact match)
  Bathrooms:   5 pts  (exact match)
  ─────────────────────────────────────
  TOTAL:     100 pts  ⭐⭐⭐⭐⭐
```

## Common Feature Codes

| Code | Name | Description |
|------|------|-------------|
| POOL | Pool | Swimming pool |
| SPA | Spa | Hot tub/spa |
| DETGAR | Detached Garage | Separate garage building |
| CARPORT | Carport | Covered parking |
| PATIO | Covered Patio | Outdoor covered area |
| SPRNK | Sprinkler | Irrigation system |
| FENCE | Fence | Property fencing |
| GAZEBO | Gazebo | Garden structure |
| TENNCT | Tennis Court | Private tennis court |
| POOLHTR | Pool Heater | Heated pool |

## Tips for Best Results

1. **For Tax Protests**: Use `min_score=70` to get only excellent matches
2. **For Market Research**: Use default `min_score=30` for broader view
3. **Dense Areas**: Reduce `max_distance=2` to focus on immediate area
4. **Rural Areas**: Increase `max_distance=10` to find enough comparables
5. **Target Count**: Adjust `max_results` based on needs (5-10 for appeals, 20+ for analysis)

## Data Requirements

Properties need these fields for accurate scoring:
- ✅ **Required**: Latitude, Longitude (for any search)
- ✅ **Recommended**: Building size, year built, bed/bath counts
- ✅ **Optional**: Extra features (pool, garage, etc.)

Missing data results in partial scoring using only available factors.

---

For detailed explanation, see [SIMILARITY_SCORING.md](SIMILARITY_SCORING.md)
