# Bedroom/Bathroom Data Missing - Investigation Complete

## Issue Summary
Property account `1074380000028` (16213 Wall St, 77040) has a BuildingDetail record but `bedrooms`, `bathrooms`, and `half_baths` fields are NULL, causing them not to display in search results.

## Root Cause
The bedroom and bathroom data exists in the source file `fixtures.txt` but was not processed during the last building data import (batch 20251111_080049).

## Verification
Run the diagnostic script to verify the issue:
```bash
docker compose exec web python scripts/diagnose_bedroom_bath.py
```

### Expected Data
From `fixtures.txt`:
- **Bedrooms**: 4 (RMB code)
- **Full Baths**: 2 (RMF code)
- **Half Baths**: 1 (RMH code)

## Solution Options

### Option 1: Quick Fix (Recommended)
Load only the room counts from fixtures.txt without re-importing all building data:

```bash
docker compose exec web python manage.py load_room_counts
```

This command:
- Reads `fixtures.txt` for RMB, RMF, RMH records
- Updates existing BuildingDetail records with bedroom/bathroom counts
- Takes ~1-2 minutes vs full import which takes 15-30 minutes

### Option 2: Full Reimport
Re-import all building data (which includes fixtures processing):

```bash
docker compose exec web python manage.py import_building_data --skip-download
```

This will:
- Re-process building_res.txt
- Re-process extra_features.txt
- **Process fixtures.txt** (the missing step)
- Takes 15-30 minutes

## Testing

### Test the Import
After running either option, verify the data was loaded:

```bash
docker compose exec web python scripts/diagnose_bedroom_bath.py
```

You should see:
```
>>> BEDROOMS: 4
>>> BATHROOMS: 2.5
>>> HALF BATHS: 1
```

### Test the UI
1. Search for "16213 Wall" in the web interface
2. Verify bedrooms and bathrooms display in results
3. Check property detail page shows correct values

### Automated Test
Run the test suite:

```bash
docker compose exec web python manage.py test data.tests.test_bedroom_bathroom_data
```

Note: The test uses a test database, so it won't see production data. To test with real data, use the diagnostic script above.

## Why This Happened

Looking at the import logs from November 11, 2025:
- The `import_building_data` command has code to process fixtures.txt
- The fixtures file exists and is readable
- Possible causes:
  1. The fixtures.txt processing step was skipped (bug)
  2. An exception occurred during fixtures processing (check logs)
  3. The import was interrupted before reaching the fixtures step

## Prevention

The monthly Celery task (`download_and_import_building_data`) should automatically process fixtures.txt. Verify it's working:

```bash
# Check task schedule
docker compose exec web python manage.py shell -c "from taxprotest.celery import app; print(app.conf.beat_schedule)"

# Check last task run
docker compose logs beat | grep building_data
```

## Technical Details

### Data Flow
1. **Source**: HCAD provides `fixtures.txt` with room counts
2. **Format**: Tab-delimited with columns: `acct`, `bld_num`, `type`, `units`
3. **Codes**: RMB (bedrooms), RMF (full baths), RMH (half baths)
4. **ETL**: `load_fixtures_room_counts()` in `data/etl.py` line 737
5. **Target**: Updates `BuildingDetail.bedrooms`, `bathrooms`, `half_baths`

### Database Schema
```python
# data/models.py - BuildingDetail
bedrooms = models.IntegerField(null=True, blank=True)
bathrooms = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
half_baths = models.IntegerField(null=True, blank=True)
```

The `bathrooms` field stores the total: `full_baths + (0.5 * half_baths)`.

### View Logic
The search results and property detail views pull from `BuildingDetail`:
- `taxprotest/views.py` line 64-65: Gets bedroom/bathroom from building
- `taxprotest/views.py` line 183-186: Formats bathroom display (e.g., "2.5")

## Next Steps

1. ✅ **Immediate**: Run `load_room_counts` command to fix missing data
2. ⏳ **Verify**: Check that search results now show bed/bath counts
3. 🔍 **Investigate**: Review logs from November 11 import to see why fixtures weren't processed
4. 📅 **Monitor**: Verify next monthly import processes fixtures correctly

## Files Modified

- `l:\TaxProtest-Django\scripts\diagnose_bedroom_bath.py` - Diagnostic script
- `l:\TaxProtest-Django\data\tests\test_bedroom_bathroom_data.py` - Test suite
- `l:\TaxProtest-Django\BEDROOM_BATHROOM_FIX.md` - This documentation

## References

- ETL Function: `data/etl.py:load_fixtures_room_counts()` (line 737)
- Import Command: `data/management/commands/import_building_data.py` (line 161)
- Room Counts Command: `data/management/commands/load_room_counts.py`
- Building Model: `data/models.py:BuildingDetail` (line 80)
- View Logic: `taxprotest/views.py` (lines 62-86, 183-207)
