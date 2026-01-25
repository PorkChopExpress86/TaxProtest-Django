# Phase 1: PPSF-Based Protest Recommendation

This document tracks the steps required to introduce a price-per-square-foot (PPSF) protest recommendation onto the Similar Properties workflow. Each step is recorded with an actionable checklist. Update the status as work is completed.

## Goals

- Surface a clear recommendation ("protest" / "consider" / "not recommended") derived solely from PPSF comparisons between a target property and its similar comps.
- Keep scope limited to existing data so implementation can be delivered quickly.
- Lay groundwork for a future phase that incorporates historical protest/hearing outcomes once that data is ingested.

## Current Understanding

- The `similar_properties` view (`taxprotest/views.py`) already computes PPSF for the target property and its comparables.
- The template (`templates/similar_properties.html`) lists PPSF but does not interpret it.
- Protest/hearing data is not yet modeled or imported, so the Phase 1 logic must rely only on PPSF statistics.

_Status: ✅ Complete_

## Implementation Summary

**Status**: ✅ **COMPLETE** - All checklist items finished

**Date Completed**: November 27, 2025

**Changes Made**:

1. **Backend (`taxprotest/views.py`)**:
   - Added PPSF calculation logic for comparable properties
   - Implemented median/average/min/max calculation with validation
   - Required minimum 3 valid comparables for recommendation
   - Created 4-tier recommendation system (strong/moderate/neutral/low)
   - Added transparent metrics (comparable count, avg score, PPSF range)

2. **UI (`templates/similar_properties.html`)**:
   - Added color-coded recommendation banner below target property card
   - Display includes recommendation, reasoning, statistics, and disclaimer
   - "Insufficient data" message for cases with < 3 valid comparables
   - Responsive design with proper color coding per recommendation level

3. **Tests (`taxprotest/tests/test_views.py`)**:
   - Added `ProtestRecommendationTests` class with 4 comprehensive tests
   - Coverage includes: strong recommendation, neutral case, insufficient data, missing PPSF handling
   - All tests passing ✅

**Implementation Decision**: Used Option A (existing `min_score` filter) for quality filtering, keeping logic simple while respecting user search parameters.

**Thresholds Used**:
- Strong (20%+ above median) → Red banner
- Moderate (10-19% above median) → Amber banner  
- Neutral (±10% of median) → Gray banner
- Low (10%+ below median) → Green banner

## Implementation Checklist

### Backend updates (`taxprotest/views.py`)

- [x] Calculate median and average PPSF for comparable properties (exclude the target row).
  - **Filtering strategy**: Only include properties with **valid PPSF data** (non-null assessed value, non-zero building area).
  - **Quality thresholds**: Apply filters to exclude weak matches:
    - **Option A (Recommended)**: Use only comparables with `similarity_score >= 50` (already filtered by algorithm's `min_score` parameter).
    - **Option B (Stricter)**: Use only comparables with `similarity_score >= 60` or top 10 matches, whichever is larger.
    - **Option C (Most conservative)**: Weight PPSF values by similarity score (higher scores = more weight in median/average calculation).
  - **Outlier handling**: Remove extreme PPSF outliers (e.g., exclude values beyond 3 standard deviations from mean, or use interquartile range filtering).
  - **Minimum sample size**: Require at least 3 valid comparable PPSF values before generating a recommendation (otherwise show "Insufficient data" message).
- [x] Derive percentage difference between the target PPSF and comparable median.
  - Use **median** as the primary comparison point (more robust against outliers than average).
  - Calculate: `over_percentage = ((target_ppsf - median_ppsf) / median_ppsf) * 100`
  - Also compute average for informational display, but base recommendation thresholds on median.
- [x] Map percentage bands to recommendation labels (e.g., Strong/Moderate/Neutral/Low) and craft rationale text.
  - Include count of properties used in calculation in the rationale (e.g., "based on 15 similar properties").
  - If using weighted approach, note the average similarity score in rationale.
- [x] Inject these new values (`protest_recommendation`, reason, stats) into the template context.
  - Add: `comparable_count`, `comparable_avg_score`, `ppsf_range` (min/max) for transparency.

### UI updates (`templates/similar_properties.html`)

- [x] Add a recommendation banner beneath the target property card with color/status cues per recommendation level.
- [x] Display the key PPSF stats (target PPSF, comp median, percentile, etc.) and a short disclaimer.

### Automated tests (`taxprotest/tests/`)

- [x] Add/extend a test ensuring the recommendation context fields appear when PPSF data exists.
- [x] Cover edge cases (e.g., missing PPSF data should skip the recommendation block).

### Manual verification & docs

- [x] Document a quick manual check (sample account, expected behavior) in this file once backend/UI changes land.
- [x] Run the relevant Django test suite (or targeted tests) and record the command/output summary here.

## Manual Verification Guide

To manually test the protest recommendation feature:

1. **Start the application**:
   ```bash
   docker compose up
   ```

2. **Navigate to a property with similar properties**:
   - Go to http://localhost:8000/
   - Search for a property (e.g., last name or ZIP code)
   - Click the "Similar" button for any property with location data

3. **Expected behavior**:
   - If the property has 3+ similar properties with valid PPSF data:
     - A recommendation banner should appear below the target property card
     - Banner color indicates recommendation level:
       - **Red border**: "Recommend protesting" (PPSF 20%+ above median)
       - **Amber border**: "Consider protesting" (PPSF 10-19% above median)
       - **Gray border**: "Borderline – depends on other factors" (PPSF within ±10% of median)
       - **Green border**: "Protest not recommended" (PPSF 10%+ below median)
     - Banner displays:
       - Recommendation text and reasoning
       - Your PPSF, median PPSF, and PPSF range
       - Number of comparables used
       - Average similarity score
       - Disclaimer about informational nature
   - If fewer than 3 similar properties with PPSF data:
     - Blue information box appears instead
     - Message suggests expanding search radius or lowering similarity threshold
   - If no similar properties found:
     - Standard "No Similar Properties Found" message displays

4. **Test different scenarios**:
   - Properties with high assessed value relative to area → Should recommend protest
   - Properties with low assessed value relative to area → Should not recommend protest
   - Properties with assessed value similar to comparables → Should show borderline
   - Properties with few comparables → Should show insufficient data message

## Test Results

**Date**: November 27, 2025

**Command**:
```bash
docker compose exec web python manage.py test taxprotest.tests.test_views.ProtestRecommendationTests
```

**Output**:
```
Found 4 test(s).
Creating test database for alias 'default'...
System check identified no issues (0 silenced).
....
----------------------------------------------------------------------
Ran 4 tests in 0.093s

OK
Destroying test database for alias 'default'...
```

**Tests Executed**:
1. `test_strong_protest_recommendation` - Verified high PPSF generates "strong" recommendation ✅
2. `test_neutral_recommendation` - Verified PPSF close to median generates "neutral" recommendation ✅
3. `test_insufficient_data_no_recommendation` - Verified fewer than 3 comparables shows no recommendation ✅
4. `test_recommendation_with_missing_ppsf` - Verified comparables without PPSF are excluded from calculation ✅

All tests passed successfully.

**Full test suite**:
```bash
docker compose exec web python manage.py test taxprotest.tests.test_views
```
Result: All 10 tests passed ✅

## Notes & Open Questions

- **Thresholds**: Proposed initial cutoffs are 20%+ (strong), 10–19% (consider), −10% to +9% (neutral), below −10% (not recommended). Adjust if stakeholders prefer different sensitivity.
- **Quality filtering decision**: Need to decide between Option A (use existing `min_score` filter), Option B (stricter threshold), or Option C (weighted calculation). **Recommendation: Start with Option A** since users already control quality via the `min_score` URL parameter (default 30), and the similarity algorithm already pre-filters candidates.
- **Sample size handling**: If fewer than 3 valid comps with PPSF data, display message: "Not enough similar properties with pricing data to make a recommendation. Try expanding your search radius."
- **Edge cases to handle**:
  - Target property missing PPSF (no building area or assessed value) → Skip recommendation entirely.
  - All comparables missing PPSF → Display "insufficient data" message.
  - PPSF outliers (e.g., $500/sqft when median is $120/sqft) → Use IQR filtering or cap at median ± 100%.
- **Display transparency**: Show users the count of properties used, their average similarity score, and PPSF range (min-max) so they understand the recommendation basis.
- Future Phase 2 will add protest/hearing success-rate data. No action required now beyond keeping the architecture flexible for additional inputs.

## Calculation Examples

### Example 1: Strong Protest Recommendation
- Target property: 2,000 sqft, $300,000 assessed → **$150/sqft**
- 12 comparables found (similarity 50-85%)
- After filtering (valid PPSF, similarity ≥50): 10 properties remain
- PPSF values: $105, $110, $115, $118, $120, $122, $125, $128, $130, $135
- **Median PPSF**: $121/sqft
- **Average PPSF**: $120.80/sqft
- **Target vs Median**: ($150 - $121) / $121 = **+24%**
- **Recommendation**: "Recommend protesting" (strong - exceeds 20% threshold)
- **Rationale**: "Your price per sqft is about 24% above the median of 10 similar properties (avg similarity score 67%)."

### Example 2: Borderline Case
- Target property: 1,800 sqft, $230,000 assessed → **$127.78/sqft**
- 8 comparables found
- After filtering: 7 properties remain
- PPSF values: $115, $118, $120, $125, $130, $132, $145
- **Median PPSF**: $125/sqft
- **Target vs Median**: ($127.78 - $125) / $125 = **+2.2%**
- **Recommendation**: "Borderline – depends on other factors" (neutral)
- **Rationale**: "Your price per sqft is close to the median of 7 similar properties."

### Example 3: Insufficient Data
- Target property: 3,500 sqft, $450,000 assessed → **$128.57/sqft**
- 4 comparables found
- After filtering (valid PPSF): Only 2 properties remain
- **Recommendation**: None shown
- **Message**: "Not enough similar properties with pricing data to make a recommendation. Try expanding your search radius or lowering the minimum similarity score."
