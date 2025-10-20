# GIS Import - In Progress

**Started:** October 19, 2025 at 10:03 PM CDT  
**Status:** 🔄 Running  
**Expected Duration:** 30-40 minutes

## Current Status

The GIS coordinate import is currently running and processing 1,523,545 parcel records from the HCAD shapefile.

### Process Details

**Command Running:**
```bash
docker compose exec -T web python manage.py load_gis_data --skip-download
```

**Log File:** `/tmp/gis_final.log`

**Current Stage:** Processing parcel records and extracting centroids

### Monitor Progress

**Option 1: Run monitoring script**
```bash
./scripts/monitor_gis_import.sh
```

**Option 2: Manual check**
```bash
docker compose exec web python manage.py shell -c "
from data.models import PropertyRecord
with_coords = PropertyRecord.objects.filter(latitude__isnull=False).count()
total = PropertyRecord.objects.count()
print(f'Progress: {with_coords:,} / {total:,} ({with_coords/total*100:.1f}%)')
"
```

**Option 3: Check log file**
```bash
tail -f /tmp/gis_final.log
```

## What's Being Imported

- **Source:** HCAD Parcels shapefile (ParcelsCity.shp - 303MB)
- **Records:** 1,523,545 parcels
- **Data:** Latitude and longitude coordinates for each property
- **Method:** Extract centroid from polygon boundaries
- **Coordinate System:** Converted from NAD83/Texas South Central to WGS84

## Expected Results

Once complete, approximately 90-95% of properties will have coordinates:

- Total properties: 1,601,376
- Expected with coordinates: ~1,450,000 - 1,520,000
- Coverage: 90-95%

Some properties may not have coordinates due to:
- New construction not yet in GIS data
- PO Box addresses (no physical location)
- Account number mismatches
- Missing parcels in HCAD GIS data

## Timeline

- **Start:** 10:03 PM (shapefile loading)
- **Stage 1:** Load and validate shapefile (5-10 minutes)
- **Stage 2:** Extract centroids from polygons (10-15 minutes) 
- **Stage 3:** Match to properties and bulk update database (15-20 minutes)
- **Expected completion:** ~10:35-10:45 PM

## Post-Import Verification

Once complete, verify the import:

```bash
# Check coverage
docker compose exec web python manage.py shell -c "
from data.models import PropertyRecord
total = PropertyRecord.objects.count()
with_coords = PropertyRecord.objects.filter(latitude__isnull=False).count()
print(f'Total: {total:,}')
print(f'With coordinates: {with_coords:,} ({with_coords/total*100:.1f}%)')
"

# Check Wall Street specifically
docker compose exec web python manage.py shell -c "
from data.models import PropertyRecord
wall_props = PropertyRecord.objects.filter(street_name='WALL', zipcode='77040')
wall_with_coords = wall_props.filter(latitude__isnull=False)
print(f'Wall Street: {wall_with_coords.count()} / {wall_props.count()} with coordinates')
if wall_with_coords.exists():
    sample = wall_with_coords.first()
    print(f'Sample: {sample.street_number} WALL')
    print(f'  Coordinates: ({sample.latitude}, {sample.longitude})')
"
```

## What Happens Next

After GIS import completes:

1. ✅ Property records will have latitude/longitude
2. ✅ Location-based similarity search will work
3. ✅ Distance calculations will be accurate
4. ✅ Map visualization will be possible (future feature)

## Future Imports

### Automated Schedule

GIS data is set to import **annually on January 15th at 3:00 AM** via Celery Beat.

### Manual Import Commands

```bash
# Download and import fresh GIS data
docker compose exec web python manage.py load_gis_data

# Use existing downloaded files
docker compose exec web python manage.py load_gis_data --skip-download

# Full import (properties + buildings + GIS)
docker compose exec web python manage.py import_all_data

# Building import with GIS
docker compose exec web python manage.py import_building_data --with-gis
```

## Troubleshooting

If import fails or hangs:

1. **Check if process is running:**
   ```bash
   ps aux | grep "load_gis_data"
   ```

2. **Check log for errors:**
   ```bash
   tail -100 /tmp/gis_final.log
   ```

3. **Restart if needed:**
   ```bash
   # Kill all GIS processes
   pkill -f "load_gis_data"
   
   # Restart import
   docker compose exec web python manage.py load_gis_data --skip-download
   ```

4. **Check Docker resources:**
   ```bash
   docker stats
   ```
   - Ensure sufficient memory (4GB+ recommended)
   - GIS import uses ~3.5GB RAM during processing

---

**Status will be updated when import completes.**
