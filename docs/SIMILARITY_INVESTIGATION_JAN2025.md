# Similarity Algorithm Investigation - January 2025

## Summary

Investigated why account **1074380000028** (16213 Wall St, 77040) returns only 5-6 poorly matched properties when searching for similar homes.

---

## Findings

### ✅ Issue #1: 500-Candidate Limit (FIXED)

**Problem**: Algorithm processed only first 500 candidates despite 381,079 properties in valid range.

**Fix Applied**: 
- Implemented batch processing (1000 per batch)
- Intelligent early termination
- Safety limit of 50,000 candidates

**Result**: Improved from 5 to 31 results

---

### 🔴 Issue #2: Missing Bedroom/Bathroom Data (CRITICAL)

**Problem**: 
- **ZERO properties** (0 of 1,301,008) have bedroom/bathroom data imported
- Data EXISTS in HCAD `fixtures.txt` but was never imported
- Costs **36 points** (18+18) out of 100 in similarity scoring
- Max possible score reduced from 100 to ~64

**Evidence**:
```
Property 1074380000028 in fixtures.txt:
- RMB (Bedrooms): 4
- RMF (Full Bath): 2  
- RMH (Half Bath): 1
→ Should be: 4 bed, 2.5 bath
→ Currently: NULL, NULL
```

**Impact**: Even with 31 results now, highest score is only 51 (should be 70-80 with bed/bath data)

---

## Test Results

| Metric | Before Fix | After Fix #1 | Expected After Fix #2 |
|--------|------------|--------------|----------------------|
| Results | 5 | 31 | 40-50 |
| Max Score | 37 | 51 | 70-80 |
| Above 50 | 0 | 1 | 20+ |
| Within 5 mi | 0 | 1 | 15+ |

---

## Next Steps

### Required: Import Bedroom/Bathroom Data

**Implementation**: Add fixtures aggregation to ETL pipeline

```python
# Aggregate fixtures.txt by account + building:
RMB → bedrooms (sum units)
RMF → full_baths (sum units)  
RMH → half_baths (sum units)
bathrooms = full_baths + (half_baths * 0.5)
```

**Files to modify**:
- `data/etl_pipeline/transform.py` - Add fixtures schema
- `data/etl_pipeline/load.py` - Join fixtures to buildings
- `data/models.py` - Already has bedroom/bathroom fields ✅

**Testing**:
1. Re-import building data with fixtures
2. Verify all properties have bed/bath counts
3. Re-test property 1074380000028
4. Expect scores 60-80+ for nearby matches

---

## Documentation Updated

- `docs/SIMILARITY_FIX.md` - Complete investigation details
- `data/similarity.py` - Added batch processing comments

---

## Quick Fix Status

✅ **Batch Processing** - Implemented  
🔄 **Fixtures Import** - Needs implementation  
📋 **Testing** - Pending fixtures import  

---

For detailed algorithm explanation, see:
- `docs/SIMILARITY_ALGORITHM.md`
- `docs/SIMILARITY_SCORING.md`
