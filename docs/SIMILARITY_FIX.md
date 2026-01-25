# Similarity Algorithm Performance Fixes

## Fix #1: 500 Server Error (Duplicates)

### Issue
The similar properties search was returning a 500 server error when accessed.

### Root Cause
The code was using `PropertyRecord.objects.get(account_number=account_number)` which expects exactly one result. However, the database contains duplicate account numbers, causing Django to raise a `MultipleObjectsReturned` exception.

### Solution
Changed both `data/similarity.py` and `taxprotest/views.py` to use `.filter().first()` instead of `.get()`:

**Before:**
```python
target = PropertyRecord.objects.get(account_number=account_number)
```

**After:**
```python
target = PropertyRecord.objects.filter(account_number=account_number).first()
```

---

## Fix #2: Poor Match Quality (January 2025)

### Problem Identified

Account `1074380000028` (16213 Wall St, 77040) returned only **5-6 results** with low similarity scores (33-37).

### Root Causes Found

#### 1. 500-Candidate Limit ✅ FIXED

**Location**: Line 267 in `data/similarity.py`

**Problem**: 
- Despite 381,079 properties in valid size range, algorithm only processed first 500
- Random sampling meant good matches were skipped
- QuerySet had no ordering, so it was essentially a random sample

**Solution Applied**:
- Implemented batch processing (1000 candidates per batch)
- Added intelligent early termination when we have 3x max_results with good scores (>= 50)
- Added absolute safety limit of 50,000 processed candidates
- Processing continues until sufficient quality results found

**Impact**:
- Results increased from **5** to **31** properties
- But scores still mediocre (max 51) due to missing bed/bath data (see below)

#### 2. Missing Bedroom/Bathroom Data 🔴 CRITICAL

**Problem**: 
- **ZERO** properties in database have bedroom/bathroom data (0 of 1,301,008)
- This data EXISTS in HCAD's `fixtures.txt` file but was never imported
- Bedroom/bathroom matching accounts for **36 points** (18 + 18) out of 100 in similarity scoring
- Without this data, max possible score drops from 100 to ~64 points

**Data Evidence**:

Property 1074380000028 in `fixtures.txt`:
```
1074380000028 1 RMB Room:  Bedroom  4.00
1074380000028 1 RMF Room:  Full Bath  2.00
1074380000028 1 RMH Room:  Half Bath  1.00
```

Should have: **4 bedrooms, 2.5 bathrooms**  
Currently in database: **NULL, NULL**

**Solution Needed**:

Parse `fixtures.txt` during building import and aggregate by building:
- `RMB` → bedrooms (sum of units)
- `RMF` → full_baths (count as 1.0 each)
- `RMH` → half_baths (count as 0.5 each)
- Total bathrooms = full_baths + (half_baths * 0.5)

**Implementation approach**:
```python
# In etl_pipeline/transform.py or new fixtures transformer
def aggregate_fixtures(fixtures_df):
    """Aggregate fixture rows into bedroom/bathroom counts"""
    grouped = fixtures_df.groupby(['account_number', 'building_number'])
    
    fixture_agg = {}
    for (acct, bldg), group in grouped:
        fixture_agg[(acct, bldg)] = {
            'bedrooms': group[group['type'] == 'RMB']['units'].sum(),
            'full_baths': group[group['type'] == 'RMF']['units'].sum(),
            'half_baths': group[group['type'] == 'RMH']['units'].sum(),
        }
    return fixture_agg
```

**Expected improvement after fix**:
- Similarity scores should increase by 20-30 points for good matches
- Property 1074380000028 should find 40+ similar properties with scores 60-80+
- Nearby properties in same neighborhood (3-5 miles) should score 70+

---

## Testing Results

### Original (500 limit + no bed/bath)
```
Found: 5 results
Scores: 33-37 (barely above min_score=30)
Distances: 8.3+ miles (near maximum radius)
```

### After Fix #1 (batch processing)
```
Found: 31 results
Scores: 30-51
Distances: 4.09-13.64 miles
Above 50: 1 property
Above 40: 10 properties
```

### Expected After Bedroom/Bath Import
```
Found: 40-50 results
Scores: 40-80
Distances: 2-10 miles
Above 50: 20+ properties
Above 40: 30+ properties
Within 5 miles: 15+ properties
```

---

## Recommendations

1. **Immediate**: Implement fixtures aggregation in ETL pipeline
2. **Data Backfill**: Re-run monthly HCAD import with fixtures data
3. **Testing**: Test property 1074380000028 again after data backfill
4. **Monitoring**: Check similarity score distribution across all properties
5. **Documentation**: Update DATABASE.md with fixtures import process

---

## Quick Reference - Scoring Weights

| Factor | Weight | Criteria |
|--------|--------|----------|
| Heated Size | 22 pts | Within 10% = max points, scales down to 50% |
| Bedrooms | 18 pts | Exact match = max points, off by 1 = 12 pts, off by 2 = 6 pts |
| Bathrooms | 18 pts | Within 0.5 = max points, within 1.0 = 12 pts, within 1.5 = 6 pts |
| Lot Size | 15 pts | Within 20% = max points, scales down |
| Quality | 12 pts | Same = 12 pts, one grade off = 6 pts |
| Features | 10 pts | Jaccard similarity of amenities (pool, garage, etc.) |
| Age | 5 pts | Within 5 years = max points, scales down |

**Total: 100 points possible**

**Current Reality Without Bed/Bath Data**: Max score ~64 points (missing 36 points)

---

## Related Files

- `data/similarity.py` - Main algorithm (FIXED: batch processing)
- `data/etl_pipeline/transform.py` - Building schema (NEEDS: fixtures aggregation)
- `extracted/Real_building_land/fixtures.txt` - Source data file
- `docs/SIMILARITY_ALGORITHM.md` - Algorithm documentation
- `docs/SIMILARITY_SCORING.md` - Detailed scoring guide

## Next Steps (Optional)
Consider adding a database constraint or cleanup script to handle duplicate account numbers more systematically in the future.
