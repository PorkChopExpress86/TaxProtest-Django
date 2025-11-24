# Search Radius Update - Summary

**Date:** November 24, 2025  
**Updated:** November 24, 2025 - Distance scoring removed

## Changes Made

### 1. Increased Search Radius
- **Changed:** Default search radius from 7 miles → 10 miles
- **Files Updated:**
  - `data/similarity.py` (line 166): `max_distance_miles: float = 10.0`
  - `taxprotest/views.py` (line 248): `max_distance = float(request.GET.get("max_distance", "10"))`

### 2. Removed Distance from Scoring (NEW)
- **Changed:** Distance no longer affects similarity score
- **Rationale:** Users want to compare properties with similar attributes (size, age, quality, etc.) and see how their price/sqft compares, regardless of distance
- **Impact:** A property 10 miles away with identical attributes now scores the same as one 1 mile away
- **Distance Still Used:** To filter candidates within the search radius (but doesn't affect score)

### 3. Redistributed Scoring Weights
The 25 points previously assigned to distance were redistributed:

**Previous Weights:**
- Distance: 25 points
- Size: 20 points
- Age: 10 points
- Quality: 10 points
- Features: 15 points
- Bedrooms: 10 points
- Bathrooms: 10 points

**Updated Weights (Revision 3 - Lot Size Added):**
- Heated Size: 22 points
- Lot Size: 15 points
- Bedrooms: 18 points
- Bathrooms: 18 points
- Quality: 12 points
- Features: 10 points
- Age: 5 points
- **Total: 100 points**

### 4. Created Comprehensive Documentation
- **New File:** `docs/SIMILARITY_ALGORITHM.md`
- **Content:** 
  - Complete algorithm explanation
  - All 6 scoring criteria with weights (distance removed)
  - Adjustment guidelines for tuning
  - Example use cases
  - Data sources and limitations

## Key Points

### Distance is Now Only a Filter
- **Search Radius:** 10 miles (default)
- **Scoring Impact:** None - distance doesn't affect similarity score
- **Purpose:** Find properties within a reasonable geographic area
- **Benefit:** Compare similar properties by attributes, not proximity

### Updated Weight Emphasis (Revision 3)
Heated size remains key but now splits importance with newly added total lot size (land area). Bedroom and bathroom remain high but slightly reduced to accommodate lot size. Quality and features are normalized; age remains low influence.

**Heated Size Bands (22 max):**
- Within 10%: 22 pts
- Within 20%: 18 pts
- Within 30%: 11 pts
- Within 50%: 6 pts

**Lot Size Bands (15 max):**
- Within 10%: 15 pts
- Within 20%: 12 pts
- Within 30%: 8 pts
- Within 50%: 4 pts

## Current Weighting Summary

| Criterion | Max Points | Weight | Priority |
|-----------|-----------|--------|----------|
| Heated Size Match | 22 | 22% | High |
| Lot Size Match | 15 | 15% | High |
| Bedroom Match | 18 | 18% | High |
| Bathroom Match | 18 | 18% | High |
| Quality Match | 12 | 12% | Medium |
| Feature Match | 10 | 10% | Medium |
| Age Match | 5 | 5% | Low |
| **TOTAL** | **100** | **100%** | |

**Distance:** Used for filtering only (max_distance_miles parameter)

## Testing Recommendations

1. **Spot check a known property:**
   ```python
   from data.similarity import find_similar_properties
   results = find_similar_properties("YOUR_ACCOUNT_NUMBER")
   print(f"Found {len(results)} similar properties")
   for r in results[:5]:
       print(f"{r['distance']:.1f}mi - Score {r['similarity_score']} - {r['property'].situs_street}")
   ```

2. **Check the web UI:**
   - Search for a property address
   - Click "Find Similar Properties"
   - Verify results now include properties up to 10 miles away
   - Check that sorting by price/sqft still works

3. **Compare score distributions:**
   - Properties 1-5 miles should score higher (get distance points)
   - Properties 5-10 miles need strong matches in other areas
   - Min score of 30 filters out poor matches

## Next Steps

Review the new documentation at `docs/SIMILARITY_ALGORITHM.md` and let me know if you want to:
- Adjust any scoring weights
- Change distance scoring bands
- Modify size/age/quality tolerances
- Add new criteria

Just tell me what changes you'd like, and I'll update the code accordingly!
