# GIS Features & Location Data

Guide to Geographic Information System (GIS) features, location data, coordinate handling, and distance calculations in TaxProtest-Django.

---

## Table of Contents

- [1. Overview](#1-overview)
- [2. Data Sources & Formats](#2-data-sources--formats)
- [3. Setup & Dependencies](#3-setup--dependencies)
- [4. Import Workflows](#4-import-workflows)
  - [Harris County GIS](#harris-county-gis)
  - [Brazos County GIS](#brazos-county-gis)
- [5. Location Queries & Distance Calculation](#5-location-queries--distance-calculation)
- [6. Scheduled Updates](#6-scheduled-updates)
- [7. Troubleshooting](#7-troubleshooting)

---

## 1. Overview

The TaxProtest-Django application uses GIS data to:
- Store property geographic coordinates (`latitude`, `longitude`).
- Enable radius bounding box candidate pre-filtering.
- Calculate accurate Haversine distances between target properties and comparables.
- Break ties among otherwise physically identical properties.
- Render maps and location-aware comparisons across Harris and Brazos counties.

---

## 2. Data Sources & Formats

### Harris County (HCAD)
- **Source:** Harris County Appraisal District GIS Data
- **URL:** `https://download.hcad.org/GIS/` (`Parcels.zip`)
- **Format:** ESRI Shapefiles (`ParcelsCity.shp`, `.dbf`, `.shx`, `.prj`)
- **Source Projection:** NAD83 / Texas South Central (EPSG:32039)
- **Target Coordinate System:** WGS84 (EPSG:4326) for web compatibility
- **Key Join Field:** `HCAD_NUM` matches `PropertyRecord.account_number`.

### Brazos County (BCAD)
- **Source:** Brazos County Appraisal District GIS shapefiles (`Parcels.shp`)
- **Target Coordinate System:** WGS84 (EPSG:4326)
- **Key Join Field:** `PROP_ID` matches `PropertyAccount.prop_id`.

---

## 3. Setup & Dependencies

### Python Packages
Core GIS libraries are installed via `requirements.txt`:
- `geopandas>=0.14.0`
- `pyogrio>=0.7.0`
- `shapely>=2.0.0`

### Database Design
- Standard PostgreSQL (no PostGIS extension required).
- Coordinates stored as `FloatField` / `DECIMAL(9, 6)` on `PropertyRecord` and `PropertyAccount`.
- Spatial bounding-box queries use indexed `(latitude, longitude)` composite indexes.

---

## 4. Import Workflows

### Harris County GIS

The authoritative full import loads and validates GIS completeness automatically:

```bash
# Full authoritative pipeline (downloads, extracts, calculates centroids, updates DB)
docker compose exec web python manage.py import_all_data

# Validate GIS coverage and data readiness
docker compose exec web python manage.py validate_data
```

#### Standalone Harris GIS Load
```bash
# Load GIS parcel centroids (downloads Parcels.zip if needed)
docker compose exec web python manage.py load_gis_data

# Skip download if files already exist in counties/harris/var/downloads/
docker compose exec web python manage.py load_gis_data --skip-download
```

### Brazos County GIS

```bash
# Ingest Brazos parcel shapefile
docker compose exec web python manage.py load_brazos_gis
```

---

## 5. Location Queries & Distance Calculation

### Bounding Box Lookup

```python
from decimal import Decimal
from counties.harris.models import PropertyRecord

# Find properties within bounding box around coordinates
southwest = (29.70, -95.45)
northeast = (29.80, -95.35)

properties = PropertyRecord.objects.filter(
    latitude__gte=southwest[0],
    latitude__lte=northeast[0],
    longitude__gte=southwest[1],
    longitude__lte=northeast[1],
    is_residential=True,
    is_data_ready=True,
)
```

### Distance Calculation

Exact distances are computed using the pure-math Haversine formula implemented in [`counties/common/similarity_math.py`](file:///home/specter/dev/TaxProtest-Django/counties/common/similarity_math.py):

```python
from counties.common.similarity_math import haversine_distance

# Property 1 (Downtown Houston) vs Property 2 (Rice University)
dist = haversine_distance(29.760427, -95.369804, 29.717208, -95.401825)
print(f"Distance: {dist:.2f} miles")
```

---

## 6. Scheduled Updates

Configured in [`taxprotest/celery.py`](file:///home/specter/dev/TaxProtest-Django/taxprotest/celery.py):

- **Schedule:** January 15th at 3:00 AM Central
- **Task:** `counties.harris.tasks_new.run_etl_pipeline` with `scope="gis-only"` and `strict=True`
- **Reason:** Parcel boundaries and centroids are updated annually by appraisal districts.

---

## 7. Troubleshooting

### Check GIS Coverage

```bash
docker compose exec web python manage.py shell -c "
from counties.harris.models import PropertyRecord
from counties.brazos.models import PropertyAccount

total_hcad = PropertyRecord.objects.count()
hcad_coords = PropertyRecord.objects.filter(latitude__isnull=False, longitude__isnull=False).count()
print(f'Harris GIS Coverage: {hcad_coords:,} / {total_hcad:,} ({hcad_coords/total_hcad*100:.1f}%)')

total_bcad = PropertyAccount.objects.count()
bcad_coords = PropertyAccount.objects.filter(latitude__isnull=False, longitude__isnull=False).count()
print(f'Brazos GIS Coverage: {bcad_coords:,} / {total_bcad:,} ({bcad_coords/total_bcad*100:.1f}%)')
"
```
