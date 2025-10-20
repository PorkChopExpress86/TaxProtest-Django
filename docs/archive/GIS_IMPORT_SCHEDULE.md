# GIS Location Data Import - Annual Scheduled Task

## Overview
This document describes the automated GIS (Geographic Information System) location data import process for the TaxProtest-Django application. The GIS import updates latitude/longitude coordinates for all property records.

## Why Annual Updates?
Property locations (lat/long coordinates) rarely change, so we only update them once per year:
- Parcels don't move
- Coordinate updates are mainly for:
  - New subdivisions
  - Parcel splits/combinations
  - GIS data corrections
- The import processes 1.5+ million records and takes 30-45 minutes

## Scheduled Import
**Schedule:** Annually on January 15th at 3:00 AM Central Time

**Task:** `data.tasks.download_and_import_gis_data`

**What it does:**
1. Downloads `Parcels.zip` from HCAD GIS data source
2. Extracts the ZIP file to `downloads/Parcels/`
3. Locates the shapefile: `Gis/pdata/ParcelsCity/ParcelsCity.shp`
4. Processes all parcels and updates PropertyRecord with:
   - `latitude`
   - `longitude`
   - `parcel_id` (if available)
5. Updates ~1.5 million property records

**Configuration:**
```python
# In taxprotest/celery.py
'download-and-import-gis-data-annually': {
    'task': 'data.tasks.download_and_import_gis_data',
    'schedule': crontab(
        month_of_year='1',      # January
        day_of_month='15',      # 15th
        hour=3,                  # 3 AM
        minute=0,
    ),
    'options': {
        'expires': 3600 * 24,   # 24 hour expiry
    }
}
```

## Manual Trigger Methods

### Method 1: Django Admin Panel (Recommended)
1. Log in to Django admin: `http://localhost:8000/admin/`
2. Navigate to "Download records" 
3. Select any record(s) (selection doesn't matter for this action)
4. From "Action" dropdown, choose "Trigger GIS location data import (manual)"
5. Click "Go"
6. You'll see a success message with the task ID
7. Monitor progress in Celery worker logs: `docker compose logs -f worker`

### Method 2: Django Management Command
```bash
docker compose exec web python manage.py load_gis_data --skip-download
```
Use `--skip-download` if you already have the Parcels.zip file extracted.

### Method 3: Celery Task (Python/Django Shell)
```python
from data.tasks import download_and_import_gis_data

# Trigger the task
task = download_and_import_gis_data.delay()
print(f"Task ID: {task.id}")

# Check status later
result = task.result
print(result)
```

## Monitoring

### Check Celery Worker Logs
```bash
docker compose logs -f worker
```

### Check Celery Beat Schedule (Scheduler)
```bash
docker compose logs -f beat
```

### Verify Task Status
Access the Celery Flower web UI (if configured) or check task results in the database.

## Import Process Details

### Data Source
- **URL:** `https://download.hcad.org/data/GIS/Parcels.zip`
- **Format:** ESRI Shapefile (.shp)
- **Size:** ~500-800 MB compressed, ~2-3 GB uncompressed
- **Records:** ~1.5 million parcels

### Processing Steps
1. **Download:** Streams large ZIP file with 10-minute timeout
2. **Extract:** Unzips to `downloads/Parcels/Gis/pdata/ParcelsCity/`
3. **Load Shapefile:** Uses GeoPandas to read GIS data
4. **Transform Coordinates:** Converts to WGS84 (EPSG:4326) if needed
5. **Calculate Centroids:** Gets center point of each parcel polygon
6. **Match & Update:** Finds properties by account number and updates lat/long
7. **Bulk Update:** Processes in batches of 5,000 for performance
8. **Skip Invalid:** Filters out parcels with NaN or missing coordinates

### Performance
- **Duration:** 30-45 minutes for full import
- **Memory:** Moderate (GeoPandas loads data in chunks)
- **Database Impact:** Bulk updates minimize lock contention
- **Progress:** Logs every 5,000 properties updated

## Error Handling

### Common Issues

**1. Shapefile Not Found**
- Check if extraction completed successfully
- Verify path: `downloads/Parcels/Gis/pdata/ParcelsCity/ParcelsCity.shp`

**2. NaN Coordinates**
- Some parcels have invalid geometry in source data
- These are automatically skipped with warnings

**3. Download Timeout**
- Large file may timeout on slow connections
- Task will retry with exponential backoff

**4. Missing GeoPandas**
- Ensure `geopandas` and `pyogrio` are installed
- These are in `requirements.txt`

## Database Impact

### Updated Fields
```python
PropertyRecord:
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    parcel_id = models.CharField(max_length=50, null=True, blank=True)
```

### Indexes
Location fields should have spatial indexes for efficient queries:
```python
class Meta:
    indexes = [
        models.Index(fields=['latitude', 'longitude']),
    ]
```

## Dependencies

```txt
geopandas>=0.14.0
pyogrio>=0.7.0
shapely>=2.0.0
```

These handle shapefile reading and geospatial operations.

## Related Files

- **Task Definition:** `data/tasks.py::download_and_import_gis_data()`
- **ETL Function:** `data/etl.py::load_gis_parcels()`
- **Management Command:** `data/management/commands/load_gis_data.py`
- **Celery Config:** `taxprotest/celery.py`
- **Admin Actions:** `data/admin.py`

## Testing the Task

### Test in Development
```bash
# Trigger manually and monitor
docker compose exec web python manage.py load_gis_data --skip-download

# Watch logs in another terminal
docker compose logs -f web
```

### Verify Data
```python
from data.models import PropertyRecord

# Check how many properties have location data
total = PropertyRecord.objects.count()
with_location = PropertyRecord.objects.filter(
    latitude__isnull=False,
    longitude__isnull=False
).count()

print(f"Properties with location: {with_location}/{total} ({with_location/total*100:.1f}%)")

# Check specific property
prop = PropertyRecord.objects.filter(
    street_name__icontains='WALL',
    site_addr_3='77040'
).first()
print(f"{prop.address}: ({prop.latitude}, {prop.longitude})")
```

## Troubleshooting

### Task Not Running
1. Check Celery Beat is running: `docker compose ps beat`
2. Check schedule: `docker compose exec beat celery -A taxprotest inspect scheduled`
3. Check timezone: Should be `America/Chicago`

### Slow Import
- Normal for 1.5M records
- Uses bulk updates every 5,000 records
- Consider running during low-traffic hours (scheduled for 3 AM)

### Incomplete Import
- Check worker logs for errors
- Verify shapefile integrity
- Ensure sufficient disk space (~3 GB for extracted files)

## Future Improvements

- [ ] Add progress bar in admin panel
- [ ] Email notification when import completes
- [ ] Compare before/after to report changes
- [ ] Archive old shapefiles for historical reference
- [ ] Add retry logic for network failures
- [ ] Implement incremental updates (only changed parcels)

---

**Last Updated:** October 16, 2025
**Next Scheduled Run:** January 15, 2026 at 3:00 AM CT
