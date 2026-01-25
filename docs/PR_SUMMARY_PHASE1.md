# PR Summary: PPSF-Based Protest Recommendation (Phase 1)

## Overview

This PR adds a price-per-square-foot (PPSF) based protest recommendation feature to the Similar Properties page. When viewing similar properties, users now see a clear recommendation on whether they should consider protesting their property tax assessment.

## What's New

### User-Facing Features

**Recommendation Banner**
- Appears below the target property card on the Similar Properties page
- Color-coded based on recommendation strength:
  - 🔴 **Red**: "Recommend protesting" (PPSF 20%+ above median)
  - 🟠 **Amber**: "Consider protesting" (PPSF 10-19% above median)
  - ⚪ **Gray**: "Borderline – depends on other factors" (PPSF within ±10% of median)
  - 🟢 **Green**: "Protest not recommended" (PPSF 10%+ below median)

**Transparent Metrics**
- Shows your PPSF vs comparable median PPSF
- Displays PPSF range (min-max) of comparables
- Lists number of properties used in calculation
- Shows average similarity score of comparables
- Includes clear disclaimer that this is informational only

**Smart Data Handling**
- Requires minimum 3 valid comparable properties with PPSF data
- Shows "insufficient data" message if too few comparables
- Automatically excludes properties without assessed values or building areas
- Uses median (not average) for robust comparison against outliers

## Technical Details

### Files Changed

1. **`taxprotest/views.py`** (backend logic)
   - Added PPSF calculation for comparable properties
   - Implemented median/average/range statistics
   - Created 4-tier recommendation system with thresholds
   - Added 9 new context variables for template

2. **`templates/similar_properties.html`** (UI)
   - Added recommendation banner with conditional styling
   - Added insufficient data message for edge cases
   - Responsive design with proper color coding

3. **`taxprotest/tests/test_views.py`** (tests)
   - Added `ProtestRecommendationTests` class
   - 4 comprehensive test cases covering all scenarios
   - All tests passing ✅

4. **`docs/protest_recommendation_phase1.md`** (documentation)
   - Comprehensive implementation plan
   - Calculation examples
   - Manual verification guide
   - Test results

### Implementation Approach

**Quality Filtering**: Uses existing `min_score` parameter (default 30) to filter comparables, respecting user's search preferences.

**Median vs Average**: Uses median PPSF as primary comparison point because it's more robust against outliers.

**Minimum Sample Size**: Requires at least 3 valid comparables to avoid unreliable recommendations.

**Calculation Formula**:
```python
over_percentage = ((target_ppsf - median_ppsf) / median_ppsf) * 100
```

### Thresholds

- **Strong** (≥20% over median) → Recommend protesting
- **Moderate** (10-19% over median) → Consider protesting
- **Neutral** (±10% of median) → Borderline/depends on other factors
- **Low** (≥10% under median) → Protest not recommended

## Testing

### Automated Tests
```bash
docker compose exec web python manage.py test taxprotest.tests.test_views.ProtestRecommendationTests
```
**Result**: 4/4 tests passing ✅

### Test Coverage
- ✅ Strong protest recommendation with high PPSF
- ✅ Neutral recommendation near median
- ✅ Insufficient data handling (< 3 comparables)
- ✅ Missing PPSF data exclusion

### Full Test Suite
```bash
docker compose exec web python manage.py test taxprotest.tests.test_views
```
**Result**: 10/10 tests passing ✅ (no regressions)

## Manual Testing

1. Start app: `docker compose up`
2. Navigate to: http://localhost:8000/
3. Search for any property with location data
4. Click "Similar" button to view comparables
5. Observe recommendation banner below target property card

## Future Enhancements (Phase 2)

Phase 2 will incorporate historical protest/hearing data once that data is imported:
- Success rates for protests in similar properties
- Neighborhood-level protest statistics
- Historical outcomes for properties with similar PPSF differentials
- More sophisticated recommendation weights

## Notes

- **Not legal/tax advice**: Feature includes clear disclaimer
- **Informational only**: Helps users make informed decisions
- **Transparent**: Shows all underlying metrics and calculations
- **Configurable**: Thresholds can be easily adjusted if needed

## Documentation

See `docs/protest_recommendation_phase1.md` for:
- Detailed implementation plan with calculation examples
- Manual verification guide
- Complete test results
- Design decisions and rationale
