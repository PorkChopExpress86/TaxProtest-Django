# Fix Applied: Similar Properties 500 Error

## Issue
The similar properties search was returning a 500 server error when accessed.

## Root Cause
The code was using `PropertyRecord.objects.get(account_number=account_number)` which expects exactly one result. However, the database contains duplicate account numbers, causing Django to raise a `MultipleObjectsReturned` exception.

## Solution
Changed both `data/similarity.py` and `taxprotest/views.py` to use `.filter().first()` instead of `.get()`:

**Before:**
```python
target = PropertyRecord.objects.get(account_number=account_number)
```

**After:**
```python
target = PropertyRecord.objects.filter(account_number=account_number).first()
```

This gracefully handles duplicates by selecting the first match and returns `None` if no match is found.

## Files Modified
1. `data/similarity.py` - Line ~161
2. `taxprotest/views.py` - Line ~203

## Testing
✅ Tested with account `0010010000013` - successfully finds 10 similar properties
✅ No more server errors

## Documentation Created
Created comprehensive documentation in `docs/SIMILARITY_SCORING.md` explaining:
- How the similarity score is calculated (0-100 scale)
- Weight of each factor (distance, size, age, features, bedrooms, bathrooms)
- Detailed examples with score breakdowns
- Use cases and interpretation guidelines

## Quick Reference - Scoring Weights

| Factor | Weight | Criteria |
|--------|--------|----------|
| Distance | 30 pts | Within 1 mile = max points, scales down to 5 miles |
| Size | 25 pts | Within 10% = max points, within 50% = 5 pts |
| Age | 15 pts | Within 2 years = max points, within 15 years = 4 pts |
| Features | 20 pts | Jaccard similarity of amenities (pool, garage, etc.) |
| Bedrooms | 5 pts | Exact match = 5 pts, off by 1 = 3 pts |
| Bathrooms | 5 pts | Within 0.5 = 5 pts, within 1.0 = 3 pts |

**Total: 100 points possible**

## Next Steps (Optional)
Consider adding a database constraint or cleanup script to handle duplicate account numbers more systematically in the future.
