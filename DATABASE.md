# Database Configuration & Data Imports

Complete guide to database management, data imports, and ETL processes for TaxProtest-Django.

## Table of Contents

- [Database Schema](#database-schema)
- [Data Sources](#data-sources)
- [Import Commands](#import-commands)
- [Import Processes](#import-processes)
- [Scheduled Imports](#scheduled-imports)
- [Data Management](#data-management)
- [Troubleshooting](#troubleshooting)

## Database Schema

### PropertyRecord Model

Main property records table with owner and valuation data.

**Fields:**
- `account_number` (PK) - HCAD account number
- `owner_name` - Property owner name
- `street_number` - Street address number
- `street_name` - Street name
- `zipcode` - ZIP code
- `assessed_value` - Property assessed value
- `building_area` - Building square footage
- `land_area` - Land square footage
- `latitude` - Geographic coordinate
- `longitude` - Geographic coordinate
- `parcel_id` - GIS parcel identifier

**Indexes:**
- account_number (primary key)
- owner_name
- street_name
- zipcode
- latitude, longitude

**Count:** ~1.6M records

### BuildingDetail Model

Detailed building specifications for each property.

**Fields:**
- `property` (FK) - Link to PropertyRecord
- `account_number` - HCAD account number (indexed)
- `building_number` - Building number (for multi-building properties)
- `building_type` - Type code
- `building_style` - Style code
- `building_class` - Class code
- `quality_code` - Quality rating
- `condition_code` - Condition rating
- `year_built` - Year of construction
- `year_remodeled` - Year of remodel
- `heat_area` - Heated square footage
- `base_area` - Base area
- `gross_area` - Gross area
- `stories` - Number of stories
- `foundation_type` - Foundation type code
- `exterior_wall` - Exterior wall material
- `roof_cover` - Roof covering material
- `roof_type` - Roof type
- `bedrooms` - Number of bedrooms
- `bathrooms` - Number of bathrooms (decimal for half baths)
- `half_baths` - Number of half bathrooms
- `fireplaces` - Number of fireplaces
- `is_active` - Active status (soft delete)
- `import_date` - Import timestamp
- `import_batch_id` - Batch identifier

**Indexes:**
- account_number, building_number
- is_active, import_date

**Count:** ~1.3M records

### ExtraFeature Model

Extra property features like pools, garages, patios.

**Fields:**
- `property` (FK) - Link to PropertyRecord
- `account_number` - HCAD account number (indexed)
- `feature_number` - Feature sequence number
- `feature_code` - Feature type code (POOL, DETGAR, etc.)
- `feature_description` - Human-readable description
- `quantity` - Number of features
- `area` - Feature area
- `length` - Feature length
- `width` - Feature width
- `quality_code` - Quality rating
- `condition_code` - Condition rating
- `year_built` - Year feature was added
- `value` - Appraised value of feature
- `is_active` - Active status (soft delete)
- `import_date` - Import timestamp
- `import_batch_id` - Batch identifier

**Indexes:**
- account_number, feature_code
- is_active, import_date

**Count:** 0-5M records (varies by import)

### DownloadRecord Model

Tracks downloaded and extracted HCAD data files.

**Fields:**
- `url` - Source URL
- `filename` - Downloaded filename
- `download_date` - When downloaded
- `extracted` - Extraction status

## Data Sources

All data comes from **Harris County Appraisal District (HCAD)**:
- Website: https://hcad.org/
- Downloads: https://download.hcad.org/data/

### File Descriptions

**Real_acct_owner.txt** (~500MB)
- Property ownership records
- Owner names and mailing addresses
- Property site addresses
- Assessed values
- Building and land areas

**Real_building_land.zip** (~1.5GB compressed, ~5GB extracted)
Contains multiple files:
- `building_res.txt` - Residential building details
- `building_other.txt` - Commercial building details
- `extra_features.txt` - Pools, garages, patios, etc.
- `fixtures.txt` - Room counts (bedrooms, bathrooms)
- `land.txt` - Land details
- `structural_elem1.txt` - Structural elements
- `structural_elem2.txt` - Additional elements

**Parcels.zip** (~800MB compressed, ~3GB extracted)
- GIS shapefiles with property boundaries
- Latitude/longitude coordinates
- Parcel identifiers

### Update Frequency

- **Real_acct_owner:** Updated monthly (around the 1st)
- **Real_building_land:** Updated monthly (around the 5th)
- **Parcels:** Updated annually (usually January)

## Import Commands

### Manual Import Commands

**Property Records (Required):**
```bash
docker compose exec web python manage.py import_hcad_data
```

**GIS Data (Recommended):**
```bash
# Download and import
docker compose exec web python manage.py load_gis_data

# Skip download (use existing files)
docker compose exec web python manage.py load_gis_data --skip-download
```

**Building Details & Features (Recommended):**
```bash
# Full import (building details, features, fixtures)
docker compose exec web python manage.py import_building_data

# Async via Celery
docker compose exec web python manage.py import_building_data --async
```

**Link Orphaned Records:**
```bash
# Link building/feature records to properties
docker compose exec web python manage.py link_orphaned_records

# Custom batch size
docker compose exec web python manage.py link_orphaned_records --chunk-size 10000
```

**Load Room Counts (Bedrooms/Bathrooms):**
```bash
docker compose exec web python manage.py load_fixtures
```

### Check Database State

```bash
docker compose exec web python manage.py shell -c "
from data.models import PropertyRecord, BuildingDetail, ExtraFeature

# Record counts
print(f'Properties: {PropertyRecord.objects.count():,}')
print(f'Buildings: {BuildingDetail.objects.count():,}')
print(f'Features: {ExtraFeature.objects.count():,}')

# Active vs inactive
print(f'Active Buildings: {BuildingDetail.objects.filter(is_active=True).count():,}')
print(f'Inactive Buildings: {BuildingDetail.objects.filter(is_active=False).count():,}')

# Properties with data
from django.db.models import Count
props_with_buildings = PropertyRecord.objects.annotate(
    bld_count=Count('buildings')
).filter(bld_count__gt=0).count()
print(f'Properties with buildings: {props_with_buildings:,}')

# Orphaned records
orphaned_buildings = BuildingDetail.objects.filter(property__isnull=True).count()
orphaned_features = ExtraFeature.objects.filter(property__isnull=True).count()
print(f'Orphaned Buildings: {orphaned_buildings:,}')
print(f'Orphaned Features: {orphaned_features:,}')
"
```

## Import Processes

### Property Records Import

**Source:** Real_acct_owner.txt  
**Duration:** 15-30 minutes  
**Records:** ~1.6M

**Process:**
1. Downloads file from HCAD
2. Parses tab-delimited text file
3. Bulk creates PropertyRecord objects (5000 per batch)
4. Creates indexes for fast lookups

**Data Imported:**
- Account numbers
- Owner names
- Site addresses (street number, name, zip)
- Assessed values
- Building and land areas

### GIS Data Import

**Source:** Parcels.zip (shapefile)  
**Duration:** 30-45 minutes  
**Records:** ~1.5M updated

**Process:**
1. Downloads Parcels.zip from HCAD GIS data
2. Extracts shapefile (ParcelsCity.shp)
3. Reads with GeoPandas
4. Converts coordinates to WGS84 (EPSG:4326)
5. Calculates parcel centroids
6. Matches by account number
7. Bulk updates PropertyRecord with lat/long

**Data Imported:**
- Latitude coordinates
- Longitude coordinates
- Parcel IDs

**Dependencies:**
- geopandas
- pyogrio
- shapely

### Building Data Import

**Source:** Real_building_land.zip  
**Duration:** 60-90 minutes  
**Records:** 1.3M buildings, 0-5M features, room counts

**Process:**
1. Downloads Real_building_land.zip
2. Extracts to downloads/Real_building_land/
3. **Soft Delete Phase:**
   - Marks all existing records as `is_active=False`
   - Preserves historical data
4. **Building Import (building_res.txt):**
   - Validates account numbers exist
   - Imports building specs (area, year, type, etc.)
   - Sets `is_active=True`, batch ID, timestamp
   - Bulk creates 5000 records per batch
5. **Feature Import (extra_features.txt):**
   - Validates account numbers
   - Imports features (pools, garages, etc.)
   - Sets import metadata
6. **Fixture Import (fixtures.txt):**
   - Loads room counts (bedrooms, bathrooms)
   - Updates existing BuildingDetail records
   - Processes RMB (bedrooms), RMF (full baths), RMH (half baths)
7. **Orphan Linking:**
   - Links records where property was NULL
   - Matches by account_number
8. **Statistics:**
   - Returns counts: imported, invalid, skipped, linked

**Data Imported:**
- Building specifications
- Year built, remodeled
- Areas (heated, base, gross)
- Quality and condition codes
- Stories, foundation, exterior, roof
- Bedrooms and bathrooms
- Extra features with descriptions

### Soft Delete & Batch Tracking

**Why Soft Deletes?**
- Preserve historical data
- Enable rollback if import fails
- Track changes between imports
- Audit data modifications

**Batch IDs:**
Each import gets unique batch ID: `YYYYMMDD_HHMMSS`
Example: `20251016_140532`

**Query by Batch:**
```python
from data.models import BuildingDetail

# Get specific import batch
batch = BuildingDetail.objects.filter(import_batch_id='20251016_140532')

# Get most recent import
from django.db.models import Max
latest_batch = BuildingDetail.objects.aggregate(Max('import_batch_id'))
latest_records = BuildingDetail.objects.filter(
    import_batch_id=latest_batch['import_batch_id__max']
)

# Reactivate old batch (rollback)
BuildingDetail.objects.filter(is_active=False).update(is_active=True)
BuildingDetail.objects.filter(import_batch_id='20251016_140532').update(is_active=False)
```

## Scheduled Imports

### Monthly Building Data Import

**Schedule:** 2nd Tuesday of each month at 2:00 AM Central  
**Task:** `data.tasks.download_and_import_building_data`  
**What:** Building details, features, fixtures (bedrooms/bathrooms)

**Configured in:** `taxprotest/celery.py`
```python
'download-and-import-building-data-monthly': {
    'task': 'data.tasks.download_and_import_building_data',
    'schedule': crontab(
        day_of_week='tuesday',
        day_of_month='8-14',  # 2nd week
        hour=2,
        minute=0,
    ),
}
```

**Why 2nd Tuesday?**
- HCAD updates data early in the month
- Gives time for HCAD to finalize updates
- Avoids 1st of month system load

### Annual GIS Import

**Schedule:** January 15th at 3:00 AM Central  
**Task:** `data.tasks.download_and_import_gis_data`  
**What:** Property coordinates (lat/long)

**Why Annually?**
- Property locations rarely change
- Large file (~800MB compressed, ~3GB extracted)
- Takes 30-45 minutes to process

### Monitor Scheduled Tasks

```bash
# Check Celery Beat scheduler logs
docker compose logs -f beat

# Check Celery worker logs
docker compose logs -f worker

# List scheduled tasks
docker compose exec beat celery -A taxprotest inspect scheduled

# Active tasks
docker compose exec worker celery -A taxprotest inspect active

# Registered tasks
docker compose exec worker celery -A taxprotest inspect registered
```

### Manual Trigger via Admin

1. Go to http://localhost:8000/admin/
2. Navigate to "Download records"
3. Select any record
4. Choose action: "Trigger building data import" or "Trigger GIS import"
5. Click "Go"
6. Monitor in worker logs: `docker compose logs -f worker`

## Data Management

### Data Validation

**Import Statistics:**
Every import returns detailed statistics:
```python
{
    'imported': 148500,      # Successfully created
    'invalid': 350,          # Invalid account numbers
    'skipped': 150,          # Missing required data
    'buildings_linked': 2450,  # Orphans linked
    'features_linked': 1830,
}
```

**Validation Checks:**
- Account number exists in PropertyRecord
- Required fields present
- Data types correct
- Duplicate detection

### Query Active Data Only

**Always filter for active records:**
```python
from data.models import BuildingDetail, ExtraFeature

# Correct - only active records
buildings = BuildingDetail.objects.filter(is_active=True)
features = ExtraFeature.objects.filter(is_active=True)

# Property relationships
property.buildings.filter(is_active=True)
property.extra_features.filter(is_active=True)
```

### Database Maintenance

**Vacuum (Optimize):**
```bash
docker compose exec db psql -U taxprotest -d taxprotest -c "VACUUM ANALYZE;"
```

**Reindex:**
```bash
docker compose exec db psql -U taxprotest -d taxprotest -c "REINDEX DATABASE taxprotest;"
```

**Check Database Size:**
```bash
docker compose exec db psql -U taxprotest -d taxprotest -c "
SELECT pg_size_pretty(pg_database_size('taxprotest')) as size;
"
```

**Table Sizes:**
```bash
docker compose exec db psql -U taxprotest -d taxprotest -c "
SELECT
  relname as table,
  pg_size_pretty(pg_total_relation_size(relid)) as size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 10;
"
```

### Backup & Restore

**Backup:**
```bash
# Full database backup
docker compose exec db pg_dump -U taxprotest taxprotest > backup_$(date +%Y%m%d).sql

# Compressed backup
docker compose exec db pg_dump -U taxprotest taxprotest | gzip > backup_$(date +%Y%m%d).sql.gz

# Schema only
docker compose exec db pg_dump -U taxprotest --schema-only taxprotest > schema.sql

# Data only
docker compose exec db pg_dump -U taxprotest --data-only taxprotest > data.sql
```

**Restore:**
```bash
# Restore full backup
docker compose exec -T db psql -U taxprotest taxprotest < backup_20251016.sql

# Restore compressed
gunzip -c backup_20251016.sql.gz | docker compose exec -T db psql -U taxprotest taxprotest
```

**Automated Backups:**
```bash
# Add to crontab
0 3 * * * cd /path/to/project && docker compose exec db pg_dump -U taxprotest taxprotest | gzip > /backups/taxprotest_$(date +\%Y\%m\%d).sql.gz
```

## Troubleshooting

### Import Fails

**Check logs:**
```bash
docker compose logs web
docker compose logs worker
```

**Common issues:**
- Disk space full: `df -h`
- Memory limit: `docker stats`
- Network timeout: Check HCAD website availability
- File corruption: Re-download and try again

**Re-run import:**
```bash
# Delete old downloads
rm -rf downloads/Real_building_land/

# Re-run import
docker compose exec web python manage.py import_building_data
```

### Orphaned Records

**Check for orphans:**
```python
from data.models import BuildingDetail, ExtraFeature

orphaned_buildings = BuildingDetail.objects.filter(property__isnull=True).count()
orphaned_features = ExtraFeature.objects.filter(property__isnull=True).count()
```

**Fix orphans:**
```bash
docker compose exec web python manage.py link_orphaned_records
```

**Why orphans exist:**
- Property record not imported yet
- Account number mismatch
- Data quality issues in source files

### Slow Queries

**Add missing indexes:**
```bash
docker compose exec web python manage.py dbshell
CREATE INDEX idx_name ON table_name (column_name);
```

**Analyze query performance:**
```bash
docker compose exec web python manage.py shell
from django.db import connection
from django.db import reset_queries

# Enable query logging
from django.conf import settings
settings.DEBUG = True

# Run your query
from data.models import PropertyRecord
props = PropertyRecord.objects.filter(zipcode='77040')

# View queries
for q in connection.queries:
    print(q['sql'])
    print(f"Time: {q['time']}s\n")
```

### Data Inconsistencies

**Find records without buildings:**
```python
from data.models import PropertyRecord
from django.db.models import Count

props_without_buildings = PropertyRecord.objects.annotate(
    bld_count=Count('buildings')
).filter(bld_count=0)
print(f"Properties without buildings: {props_without_buildings.count()}")
```

**Find duplicate account numbers:**
```python
from data.models import BuildingDetail
from django.db.models import Count

dupes = BuildingDetail.objects.values('account_number', 'building_number').annotate(
    count=Count('id')
).filter(count__gt=1)
print(f"Duplicate buildings: {dupes.count()}")
```

### CSV Field Size Limit

If you see `_csv.Error: field larger than field limit`:

**Fixed in code:**
```python
import csv
csv.field_size_limit(10485760)  # 10MB limit
```

This is already set in `data/etl.py`.

---

**For more information:**
- [SETUP.md](SETUP.md) - Installation and configuration
- [GIS.md](GIS.md) - GIS data and location features
- [README.md](README.md) - Main documentation
