# GIS Features & Location Data

Complete guide to Geographic Information System (GIS) features, location data, and coordinate handling in TaxProtest-Django.

## Table of Contents

- [Overview](#overview)
- [Data Sources](#data-sources)
- [Setup & Dependencies](#setup--dependencies)
- [Import Process](#import-process)
- [Location Features](#location-features)
- [Similarity Search](#similarity-search)
- [Scheduled Updates](#scheduled-updates)
- [Troubleshooting](#troubleshooting)

## Overview

The TaxProtest-Django application uses GIS data to:
- Store property coordinates (latitude/longitude)
- Enable location-based searches
- Calculate distances between properties
- Weight property similarity by proximity
- Display properties on maps

**Key Benefits:**
- Accurate property locations
- Distance-based comparisons
- Neighborhood analysis
- Map visualizations

**Status:** ✅ Implemented and operational

## Data Sources

### HCAD Parcels Shapefile

**Source:** Harris County Appraisal District (HCAD)  
**URL:** https://download.hcad.org/GIS/  
**File:** Parcels.zip (~800MB compressed, ~3GB extracted)

**Contents:**
- `ParcelsCity.shp` - Main shapefile with property boundaries
- `ParcelsCity.shx` - Shape index file
- `ParcelsCity.dbf` - Attribute database
- `ParcelsCity.prj` - Projection information
- `ParcelsCity.cpg` - Character encoding

**Update Frequency:** Annually (usually January)

**Coordinate System:**
- **Source:** NAD83 / Texas South Central (EPSG:32039)
- **Converted to:** WGS84 (EPSG:4326) for web mapping compatibility

### Shapefile Attributes

**Key Fields:**
- `HCAD_NUM` - Account number (matches PropertyRecord.account_number)
- `SITE_ADDR_` - Site address
- `OWNER` - Owner name
- `geometry` - Polygon boundary

**Processing:**
- Extract parcel centroid from polygon
- Convert coordinates to WGS84
- Store latitude/longitude in PropertyRecord

## Setup & Dependencies

### Python Packages

**Core GIS Libraries:**
```
geopandas>=0.14.0
pyogrio>=0.7.0
shapely>=2.0.0
```

**Install:**
```bash
pip install geopandas pyogrio shapely
```

**Docker:** Already included in `requirements.txt`

### Database Requirements

**PostgreSQL Extensions:**
- No PostGIS required! Using simple latitude/longitude fields
- Standard PostgreSQL is sufficient
- Coordinates stored as DECIMAL(9, 6) fields

**Schema:**
```sql
ALTER TABLE data_propertyrecord
ADD COLUMN latitude DECIMAL(9, 6),
ADD COLUMN longitude DECIMAL(9, 6),
ADD COLUMN parcel_id VARCHAR(50);

CREATE INDEX idx_propertyrecord_lat_long 
ON data_propertyrecord (latitude, longitude);
```

### System Dependencies

**Ubuntu/Debian:**
```bash
apt-get install -y gdal-bin libgdal-dev libspatialindex-dev
```

**macOS:**
```bash
brew install gdal spatialindex
```

**Docker:** Already configured in Dockerfile

## Import Process

### Initial Import

**Step 1: Download and Import**
```bash
docker compose exec web python manage.py load_gis_data
```

**What it does:**
1. Downloads Parcels.zip from HCAD (~800MB)
2. Extracts to `downloads/Parcels/`
3. Reads shapefile with GeoPandas
4. Converts coordinates to WGS84
5. Calculates polygon centroids
6. Matches by account number
7. Bulk updates PropertyRecord
8. Creates lat/long indexes

**Duration:** 30-45 minutes  
**Records Updated:** ~1.5M properties

**Step 2: Verify Import**
```bash
docker compose exec web python manage.py shell -c "
from data.models import PropertyRecord

total = PropertyRecord.objects.count()
with_coords = PropertyRecord.objects.filter(
    latitude__isnull=False,
    longitude__isnull=False
).count()

print(f'Total properties: {total:,}')
print(f'With coordinates: {with_coords:,}')
print(f'Coverage: {with_coords/total*100:.1f}%')
"
```

**Expected Coverage:** 90-95% (some properties lack parcel data)

### Re-import (Update Coordinates)

**Skip download if file exists:**
```bash
docker compose exec web python manage.py load_gis_data --skip-download
```

**Force fresh download:**
```bash
# Remove old files
rm -rf downloads/Parcels/

# Download and import
docker compose exec web python manage.py load_gis_data
```

### Manual Download

If automatic download fails:

1. **Download manually:**
   - Go to: https://download.hcad.org/GIS/
   - Download: Parcels.zip
   - Place in: `downloads/Parcels.zip`

2. **Extract:**
   ```bash
   mkdir -p downloads/Parcels
   unzip downloads/Parcels.zip -d downloads/Parcels/
   ```

3. **Import:**
   ```bash
   docker compose exec web python manage.py load_gis_data --skip-download
   ```

## Location Features

### Coordinate Storage

**Model Fields:**
```python
class PropertyRecord(models.Model):
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="Property latitude (WGS84)"
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="Property longitude (WGS84)"
    )
    parcel_id = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        help_text="GIS parcel identifier"
    )
```

**Example Coordinates:**
- Latitude: 29.760427 (Houston area: ~29.5 to 30.1)
- Longitude: -95.369804 (Houston area: ~-95.8 to -95.0)

### Query by Location

**Find properties near coordinates:**
```python
from data.models import PropertyRecord
from decimal import Decimal

# Center point
center_lat = Decimal('29.760427')
center_lng = Decimal('-95.369804')

# Search within ~0.01 degrees (~1.1 km)
nearby = PropertyRecord.objects.filter(
    latitude__gte=center_lat - Decimal('0.01'),
    latitude__lte=center_lat + Decimal('0.01'),
    longitude__gte=center_lng - Decimal('0.01'),
    longitude__lte=center_lng + Decimal('0.01')
)
```

**Find properties in bounding box:**
```python
# Houston bounding box
southwest = (Decimal('29.5'), Decimal('-95.8'))
northeast = (Decimal('30.1'), Decimal('-95.0'))

properties = PropertyRecord.objects.filter(
    latitude__gte=southwest[0],
    latitude__lte=northeast[0],
    longitude__gte=southwest[1],
    longitude__lte=northeast[1]
)
```

### Distance Calculation

**Haversine formula** (used in similarity search):
```python
import math

def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate distance between two points on Earth in miles.
    Uses Haversine formula.
    """
    R = 3958.8  # Earth radius in miles
    
    lat1_rad = math.radians(float(lat1))
    lat2_rad = math.radians(float(lat2))
    delta_lat = math.radians(float(lat2 - lat1))
    delta_lon = math.radians(float(lon2 - lon1))
    
    a = (math.sin(delta_lat / 2) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) *
         math.sin(delta_lon / 2) ** 2)
    c = 2 * math.asin(math.sqrt(a))
    
    return R * c
```

**Example:**
```python
from data.similarity import haversine_distance

# Property 1: Downtown Houston
lat1, lon1 = 29.760427, -95.369804

# Property 2: Rice University
lat2, lon2 = 29.717208, -95.401825

distance = haversine_distance(lat1, lon1, lat2, lon2)
print(f"Distance: {distance:.2f} miles")  # ~3.2 miles
```

## Similarity Search

### Location-Based Weighting

The similarity algorithm uses distance to weight property comparisons:

**Distance Ranges:**
- 0-2 miles: Weight 1.0 (same neighborhood)
- 2-5 miles: Weight 0.5 (nearby area)
- 5-10 miles: Weight 0.2 (same region)
- 10+ miles: Weight 0.0 (too far for comparison)

**Formula:**
```python
def get_distance_weight(distance_miles):
    if distance_miles <= 2:
        return 1.0
    elif distance_miles <= 5:
        return 0.5
    elif distance_miles <= 10:
        return 0.2
    else:
        return 0.0
```

### Similarity Score Calculation

**Weighted components:**
```python
score = (
    area_score * 0.3 +           # Building area similarity (30%)
    value_score * 0.25 +          # Property value similarity (25%)
    location_score * 0.20 +       # Distance proximity (20%)
    features_score * 0.15 +       # Feature match (15%)
    condition_score * 0.10        # Condition/quality (10%)
)
```

**Location score:**
- Based on distance weight
- Properties without coordinates: score = 0
- Close properties get higher scores
- Encourages neighborhood comparisons

### Example Query

**Find similar properties within 5 miles:**
```python
from data.similarity import find_similar_properties

property = PropertyRecord.objects.get(account_number='0040170000016')
similar = find_similar_properties(property, limit=10)

for prop, score in similar:
    if prop.latitude and prop.longitude and property.latitude and property.longitude:
        distance = haversine_distance(
            property.latitude, property.longitude,
            prop.latitude, prop.longitude
        )
        print(f"{prop.street_name}: Score={score:.2f}, Distance={distance:.2f}mi")
```

## Scheduled Updates

### Annual GIS Import

**Schedule:** January 15th at 3:00 AM Central  
**Task:** `data.tasks.download_and_import_gis_data`  
**Configured in:** `taxprotest/celery.py`

```python
'download-and-import-gis-data-annually': {
    'task': 'data.tasks.download_and_import_gis_data',
    'schedule': crontab(
        month_of_year=1,
        day_of_month=15,
        hour=3,
        minute=0,
    ),
}
```

**Why Annually?**
- Property locations rarely change
- Large file download (~800MB)
- Long processing time (30-45 minutes)
- HCAD updates once per year

**Monitor task:**
```bash
# Check Celery Beat logs
docker compose logs -f beat

# Check worker processing
docker compose logs -f worker

# View scheduled tasks
docker compose exec beat celery -A taxprotest inspect scheduled
```

### Manual Trigger

**Via Management Command:**
```bash
docker compose exec web python manage.py load_gis_data
```

**Via Django Admin:**
1. Go to: http://localhost:8000/admin/
2. Navigate to: Download records
3. Select any record
4. Action: "Trigger GIS data import"
5. Click "Go"
6. Monitor: `docker compose logs -f worker`

**Via Celery Task:**
```python
from data.tasks import download_and_import_gis_data

# Queue task
download_and_import_gis_data.delay()
```

## Troubleshooting

### Missing Coordinates

**Check coverage:**
```python
from data.models import PropertyRecord

with_coords = PropertyRecord.objects.filter(
    latitude__isnull=False
).count()
without_coords = PropertyRecord.objects.filter(
    latitude__isnull=True
).count()

print(f"With coordinates: {with_coords:,}")
print(f"Without coordinates: {without_coords:,}")
```

**Common reasons for missing coordinates:**
- Property not in HCAD parcel shapefile
- New construction not yet in GIS data
- PO Box addresses (no physical location)
- Account number mismatch

**Fix:**
Wait for next HCAD GIS update or check HCAD GIS portal manually.

### Import Errors

**GDAL/OGR errors:**
```
ERROR: Could not open shapefile
```

**Fix:**
```bash
# Check GDAL version
docker compose exec web python -c "import osgeo; print(osgeo.__version__)"

# Check file exists
docker compose exec web ls -lh downloads/Parcels/

# Verify shapefile integrity
docker compose exec web ogrinfo downloads/Parcels/ParcelsCity.shp
```

**Projection errors:**
```
ERROR: Invalid projection
```

**Fix:**
```bash
# Check projection
docker compose exec web python -c "
import geopandas as gpd
gdf = gpd.read_file('downloads/Parcels/ParcelsCity.shp')
print(gdf.crs)
"

# Should output: EPSG:32039
```

### Memory Issues

**Out of memory during import:**
```
MemoryError or killed by OOM
```

**Fix:**
```bash
# Increase Docker memory limit
# Edit docker-compose.yml:
services:
  web:
    mem_limit: 4g
    memswap_limit: 4g

# Or process in smaller chunks (modify management command)
```

**Monitor memory:**
```bash
docker stats
```

### Coordinate Validation

**Check coordinate ranges:**
```python
from data.models import PropertyRecord

# Houston area bounds
invalid_coords = PropertyRecord.objects.filter(
    latitude__isnull=False
).exclude(
    latitude__gte=29.0,
    latitude__lte=30.5,
    longitude__gte=-96.0,
    longitude__lte=-95.0
)

if invalid_coords.exists():
    print(f"Found {invalid_coords.count()} properties with invalid coordinates")
    for prop in invalid_coords[:5]:
        print(f"{prop.account_number}: ({prop.latitude}, {prop.longitude})")
```

### Distance Calculation Issues

**Test distance calculation:**
```python
from data.similarity import haversine_distance

# Known distance: Downtown to IAH airport (~23 miles)
downtown = (29.760427, -95.369804)
iah = (29.984433, -95.341442)

distance = haversine_distance(
    downtown[0], downtown[1],
    iah[0], iah[1]
)

print(f"Distance: {distance:.2f} miles")
# Should be ~23 miles

assert 22 < distance < 24, "Distance calculation incorrect!"
```

### Slow Queries

**Add indexes:**
```sql
CREATE INDEX idx_propertyrecord_lat_long 
ON data_propertyrecord (latitude, longitude);

CREATE INDEX idx_propertyrecord_lat 
ON data_propertyrecord (latitude);

CREATE INDEX idx_propertyrecord_long 
ON data_propertyrecord (longitude);
```

**Query optimization:**
```python
# Bad: Loads all records
properties = PropertyRecord.objects.filter(latitude__isnull=False)
for prop in properties:
    if is_near(prop.latitude, prop.longitude):
        nearby.append(prop)

# Good: Filter in database
nearby = PropertyRecord.objects.filter(
    latitude__gte=min_lat,
    latitude__lte=max_lat,
    longitude__gte=min_lng,
    longitude__lte=max_lng
)
```

### Shapefile Version Changes

If HCAD changes shapefile structure:

1. **Download new file manually**
2. **Inspect structure:**
   ```bash
   docker compose exec web ogrinfo -al downloads/Parcels/ParcelsCity.shp | head -100
   ```
3. **Check field names:**
   ```python
   import geopandas as gpd
   gdf = gpd.read_file('downloads/Parcels/ParcelsCity.shp', rows=5)
   print(gdf.columns.tolist())
   ```
4. **Update management command** if fields changed
5. **Test import** on small dataset

## Future Enhancements

### Potential Improvements

**Map Visualization:**
- Integrate Leaflet or Google Maps
- Display properties on interactive map
- Color code by similarity score
- Click property for details

**Advanced Location Features:**
- School district boundaries
- Flood zone data
- Neighborhood boundaries
- Distance to amenities (parks, schools, transit)

**Performance Optimization:**
- PostGIS extension for spatial queries
- Spatial indexes (R-tree)
- Pre-computed distance matrices
- Cached distance calculations

**Data Validation:**
- Reverse geocoding to verify addresses
- Parcel boundary display
- Lot size from polygon area
- Address standardization

---

**For more information:**
- [DATABASE.md](DATABASE.md) - Data imports and management
- [SETUP.md](SETUP.md) - Installation and configuration
- [README.md](README.md) - Main documentation
