# GIS Data Setup Guide

## Overview
This guide explains how to download and import GIS parcel data from Harris County Appraisal District (HCAD) to enable location-based features.

## What Gets Imported
The GIS Parcels data includes:
- **Latitude/Longitude**: Geographic coordinates for each parcel centroid
- **Parcel ID**: Unique parcel identifier from HCAD
- **Account Number**: Links GIS data to existing property records

## Prerequisites
The following Python packages are required (already in `requirements.txt`):
- `geopandas>=0.14` - For processing shapefiles
- `pyogrio>=0.7` - Fast shapefile I/O

## Import Process

### Step 1: Download and Load GIS Data
Run the management command to download and process the Parcels.zip file:

```bash
docker compose exec web python manage.py load_gis_data
```

This command will:
1. Download `Parcels.zip` from https://download.hcad.org/data/GIS/Parcels.zip
2. Extract the shapefile
3. Read parcel geometries and calculate centroids
4. Match parcels to existing PropertyRecord entries by account number
5. Update latitude, longitude, and parcel_id fields

### Step 2: Verify Import
Check that coordinates were added:

```bash
docker compose exec web python manage.py shell
```

```python
from data.models import PropertyRecord

# Count records with coordinates
with_coords = PropertyRecord.objects.filter(latitude__isnull=False, longitude__isnull=False).count()
total = PropertyRecord.objects.count()
print(f"Properties with coordinates: {with_coords} / {total}")

# Show a sample
sample = PropertyRecord.objects.filter(latitude__isnull=False).first()
print(f"Sample: {sample.address} at ({sample.latitude}, {sample.longitude})")
```

## Database Schema Updates
The following fields were added to `PropertyRecord` model:

- `latitude` (DecimalField): Latitude coordinate (WGS84)
- `longitude` (DecimalField): Longitude coordinate (WGS84)
- `parcel_id` (CharField): HCAD parcel identifier

All three fields are indexed for efficient spatial queries.

## Next Steps: Property Features
To enable "Find Similar Properties" functionality, we also need to import:

1. **Building Details** (from `Real_building_land.txt` or similar):
   - Year built
   - Building quality/condition
   - Number of stories
   - Building style/type

2. **Extra Features** (from `Real_acct_extr_feat.txt` or similar):
   - Pools (and pool heaters)
   - Spas
   - Detached garages
   - Carports
   - Other amenities

These will be imported in a separate step and linked to PropertyRecord via account number.

## Troubleshooting

### "No shapefile found"
The Parcels.zip should contain a `.shp` file. Check the `downloads/Parcels/` directory to verify extraction.

### "Could not find account number column"
The shapefile must have an account number field (typically `HCAD_NUM`, `ACCT`, or similar). Run with `--help` to see available options.

### Performance
Processing 1M+ parcels can take 10-30 minutes depending on hardware. Progress is printed every 5000 records.
