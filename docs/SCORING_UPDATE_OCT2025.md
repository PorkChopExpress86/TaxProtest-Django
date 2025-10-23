# Similarity Scoring Updates - October 2025

## Changes Made

### 1. **Adjusted Scoring Weights**
Updated the similarity scoring algorithm to give more importance to bedroom/bathroom matches and added quality code matching.

#### New Scoring Weights (Total: 100 points)

| Factor | Old Weight | New Weight | Change | Rationale |
|--------|-----------|-----------|---------|-----------|
| **Distance** | 30 pts | 25 pts | -5 | Reduced to make room for quality |
| **Size Match** | 25 pts | 20 pts | -5 | Reduced slightly |
| **Age Match** | 15 pts | 10 pts | -5 | Reduced to prioritize rooms |
| **Quality Match** | - | **10 pts** | +10 | **NEW** - Quality code matters! |
| **Feature Match** | 20 pts | 15 pts | -5 | Reduced to balance scoring |
| **Bedroom Match** | 5 pts | **10 pts** | +5 | **DOUBLED** - More important |
| **Bathroom Match** | 5 pts | **10 pts** | +5 | **DOUBLED** - More important |

**Total:** 100 points (unchanged)

---

## Updated Scoring Details

### Distance (25 points max) - *Reduced from 30*
```
Within 1 mile:  25 pts  (was 30)
Within 2 miles: 18 pts  (was 20)
Within 3 miles: 10 pts  (unchanged)
Within 5 miles:  5 pts  (unchanged)
```

### Size Match (20 points max) - *Reduced from 25*
```
Within 10%: 20 pts  (was 25)
Within 20%: 16 pts  (was 20)
Within 30%: 10 pts  (unchanged)
Within 50%:  5 pts  (unchanged)
```

### Age Match (10 points max) - *Reduced from 15*
```
±2 years:  10 pts  (was 15)
±5 years:   8 pts  (was 12)
±10 years:  5 pts  (was 8)
±15 years:  3 pts  (was 4)
```

### **Quality Match (10 points max) - NEW!**
Based on HCAD quality codes that indicate construction quality and finishes:

```
Quality Codes:
  X = Superior    (highest)
  A = Excellent
  B = Good
  C = Average
  D = Low
  E = Very Low
  F = Poor

Scoring:
  Exact match:         10 pts  ⭐⭐⭐
  One level off:        7 pts  ⭐⭐
  Two levels off:       4 pts  ⭐
  More than 2 off:      0 pts
```

**Examples:**
- Target: A (Excellent), Candidate: A (Excellent) → **10 points**
- Target: B (Good), Candidate: A (Excellent) → **7 points**
- Target: B (Good), Candidate: C (Average) → **7 points**
- Target: A (Excellent), Candidate: C (Average) → **4 points**
- Target: X (Superior), Candidate: D (Low) → **0 points**

### Feature Match (15 points max) - *Reduced from 20*
Jaccard similarity of amenities (pool, spa, garage, etc.)
```
Score = (matching_features / total_unique_features) × 15
```

### **Bedroom Match (10 points max) - DOUBLED!**
```
Exact match:      10 pts  ⭐⭐⭐  (was 5)
Off by 1 room:     6 pts  ⭐⭐   (was 3)
Off by 2 rooms:    3 pts  ⭐    (NEW)
More than 2 off:   0 pts
```

### **Bathroom Match (10 points max) - DOUBLED!**
```
Within 0.5 baths: 10 pts  ⭐⭐⭐  (was 5)
Within 1.0 baths:  6 pts  ⭐⭐   (was 3)
Within 1.5 baths:  3 pts  ⭐    (NEW)
More than 1.5:     0 pts
```

---

## Quality Code Display

### Added Quality Code Column
Quality codes now appear in:
1. **Search Results Table** - Shows quality code with color-coded badges
2. **Similar Properties Table** - Shows quality code for each result
3. **Target Property Details** - Shows quality code in property summary
4. **CSV Export** - Includes quality code column

### Color Coding
- 🟣 **X (Superior)** - Purple badge
- 🟢 **A (Excellent)** - Green badge
- 🔵 **B (Good)** - Blue badge
- 🟡 **C (Average)** - Yellow badge
- 🟠 **D (Low)** - Orange badge
- ⚪ **E/F (Very Low/Poor)** - Gray badge

Hover over any quality badge to see the full description.

---

## Example Score Comparison

### Before vs After - Same Property Match

**Scenario:** Comparing two 3-bed/2-bath homes

#### Before (Old Weights):
```
Target:    3 bed, 2.5 bath, 2000 sqft, Built 2005, Good quality
Candidate: 3 bed, 2.0 bath, 2100 sqft, Built 2006, Excellent quality

Distance:   30 pts  (0.5 miles)
Size:       25 pts  (5% difference)
Age:        15 pts  (1 year)
Features:   15 pts  (75% match)
Bedrooms:    5 pts  (exact)
Bathrooms:   3 pts  (0.5 difference)
─────────────────────────────
TOTAL:      93 pts
```

#### After (New Weights):
```
Target:    3 bed, 2.5 bath, 2000 sqft, Built 2005, Good (B) quality
Candidate: 3 bed, 2.0 bath, 2100 sqft, Built 2006, Excellent (A) quality

Distance:   25 pts  (0.5 miles)
Size:       20 pts  (5% difference)
Age:        10 pts  (1 year)
Quality:     7 pts  (one level off: B vs A)
Features:   11 pts  (75% match)
Bedrooms:   10 pts  (exact match)
Bathrooms:  10 pts  (0.5 difference)
─────────────────────────────
TOTAL:      93 pts
```

**Result:** Similar total score, but now accounts for quality and gives more weight to room counts!

---

## Impact Analysis

### Properties That Score Higher Now:
✅ Properties with **matching quality codes**
✅ Properties with **exact bedroom matches**
✅ Properties with **exact bathroom matches**
✅ Properties in **similar quality tier** (A/B/C)

### Properties That Score Lower Now:
❌ Properties that are close but have **mismatched quality** (e.g., Superior vs Average)
❌ Properties that are close but have **different room counts**
❌ Properties that relied heavily on distance alone

### Overall Effect:
The new scoring better reflects what makes properties truly comparable for tax appeals and market analysis. **Quality and room counts are now as important as location and size.**

---

## Files Modified

1. **`data/similarity.py`**
   - Updated `calculate_similarity_score()` function
   - Added quality code matching logic
   - Adjusted all scoring weights

2. **`taxprotest/views.py`**
   - Added `quality_code` to search results
   - Added `quality_code` to similar properties results
   - Added `quality_code` to CSV export
   - Added `target_quality_code` to context

3. **`templates/index.html`**
   - Added Quality column to search results table
   - Added color-coded quality badges with tooltips

4. **`templates/similar_properties.html`**
   - Added Quality column to similar properties table
   - Added quality code to target property details
   - Added color-coded quality badges with tooltips

---

## Testing

To test the new scoring:
```bash
docker compose restart web
```

Then search for properties and click "Find Similar" to see:
- New quality code column
- Updated similarity scores reflecting new weights
- Better matching based on room counts and quality

---

## Future Enhancements

Potential improvements:
1. **Quality Preference**: Allow users to filter by minimum quality code
2. **Weighted Custom Scoring**: Let users adjust factor weights
3. **Quality Trend Analysis**: Show quality distribution in neighborhoods
4. **Quality Impact on Value**: Analyze price per sqft by quality tier

---

## See Also
- [SIMILARITY_SCORING.md](SIMILARITY_SCORING.md) - Full scoring algorithm documentation
- [SIMILARITY_QUICKREF.md](SIMILARITY_QUICKREF.md) - Quick reference guide
- [desc_r_07_quality_code.txt](../downloads/Code_description_real/desc_r_07_quality_code.txt) - HCAD quality code definitions
