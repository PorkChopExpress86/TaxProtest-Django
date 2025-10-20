# Building Details and Extra Features Import Guide

## Overview
This guide explains how to import building details and extra features data from HCAD to enable the "Find Similar Properties" feature.

## What Gets Imported

### Building Details (from `building_res.txt`)
- **Building identification**: Type, style, class
- **Quality and condition**: Quality code, condition code
- **Age**: Year built, year remodeled, effective year
- **Areas**: Living area (heat_area), base area, gross area
- **Construction**: Stories, foundation type, exterior wall, roof type/cover
- **Rooms**: Bedrooms, bathrooms, half baths, fireplaces

### Extra Features (from `extra_features.txt`)
Common feature codes:
- **POOL** - Swimming pool
- **POOLHTR** - Pool heater
- **SPA** - Spa/hot tub
- **DETGAR** - Detached garage
- **CARPORT** - Carport
- **PATIO** - Patio cover
- **FENCE** - Fencing
- **SPRNK** - Sprinkler system
- And many more...

Each feature includes:
- Feature code and description
- Quantity, area, dimensions
- Quality and condition codes
- Year built
- Appraised value

## Database Schema

### BuildingDetail Model
- Linked to `PropertyRecord` via foreign key and `account_number`
- Indexed on `account_number` and `building_number` for fast lookups
- One property can have multiple buildings (main house + guest house, etc.)

### ExtraFeature Model
- Linked to `PropertyRecord` via foreign key and `account_number`
- Indexed on `account_number` and `feature_code`
- One property can have many extra features

## Prerequisites

1. **Real_building_land.zip must be downloaded and extracted**
   ```bash
   # This should contain:
   # - building_res.txt (residential buildings)
   # - building_other.txt (commercial/other buildings)
   # - extra_features.txt (pools, garages, etc.)
   # - extra_features_detail1.txt
   # - extra_features_detail2.txt
   # - land.txt
   # - fixtures.txt
   # - etc.
   ```

2. **PropertyRecord data must be loaded first**
   - The import process links buildings/features to existing properties
   - Properties without matching account numbers will have `property=None`

## Import Process

### Step 1: Download and Extract Data
If not already done:
```bash
docker compose exec web python manage.py download_and_extract_hcad
```
Or manually download from: https://download.hcad.org/data/CAMA/2025/Real_building_land.zip

### Step 2: Load Building Details and Features
```bash
docker compose exec web python manage.py load_building_features
```

This command will:
1. Load all residential building records from `building_res.txt`
2. Load all extra features from `extra_features.txt`
3. Link them to existing PropertyRecord entries by account number
4. Show progress every 5000 records

### Step 3: Verify Import
Check that data was loaded:
```bash
docker compose exec web python manage.py shell
```

```python
from data.models import PropertyRecord, BuildingDetail, ExtraFeature

# Count buildings and features
buildings = BuildingDetail.objects.count()
features = ExtraFeature.objects.count()
print(f"Buildings: {buildings}, Features: {features}")

# Find properties with pools
pools = ExtraFeature.objects.filter(feature_code__icontains='POOL').count()
print(f"Properties with pools: {pools}")

# Sample building detail
sample = BuildingDetail.objects.filter(year_built__isnull=False).first()
if sample:
    print(f"\nSample Building:")
    print(f"  Account: {sample.account_number}")
    print(f"  Year Built: {sample.year_built}")
    print(f"  Living Area: {sample.heat_area} sqft")
    print(f"  Bedrooms: {sample.bedrooms}")
    print(f"  Bathrooms: {sample.bathrooms}")
    print(f"  Stories: {sample.stories}")
    
# Sample with features
prop = PropertyRecord.objects.filter(
    extra_features__feature_code__icontains='POOL'
).first()
if prop:
    print(f"\nProperty with pool: {prop.address}")
    for feat in prop.extra_features.all():
        print(f"  - {feat.feature_description} ({feat.feature_code})")
```

## Command Options

### Load specific files
```bash
# Custom file paths
docker compose exec web python manage.py load_building_features \
    --building-file downloads/Real_building_land/building_res.txt \
    --features-file downloads/Real_building_land/extra_features.txt
```

### Skip certain imports
```bash
# Only load buildings, skip features
docker compose exec web python manage.py load_building_features --skip-features

# Only load features, skip buildings
docker compose exec web python manage.py load_building_features --skip-buildings
```

## Performance Notes

- **Building Details**: ~1-2 million records, takes 5-15 minutes
- **Extra Features**: ~3-5 million records, takes 15-30 minutes
- Batch size: 5000 records per transaction for optimal performance
- Progress is printed every 5000 records
- Uses `bulk_create()` with `ignore_conflicts=True` for speed

## Next Steps: Similar Properties Search

With building details and features loaded, you can now:

1. **Find properties by features**:
   - Properties with pools
   - Properties with specific square footage
   - Properties built in a certain year range

2. **Implement similarity algorithm**:
   - Match by location (lat/long within X miles)
   - Match by size (building area ±20%)
   - Match by age (year built ±5 years)
   - Match by features (pool, garage, etc.)
   - Match by bedrooms/bathrooms

3. **Add "Find Similar" button** to search results
   - Shows properties with similar characteristics
   - Ranked by similarity score
   - Displays matching features

## Troubleshooting

### "Building file not found"
Make sure `Real_building_land.zip` has been downloaded and extracted to `downloads/Real_building_land/`

### "Property matching query does not exist"
This is normal - the import will create BuildingDetail/ExtraFeature records even if the PropertyRecord doesn't exist yet. The foreign key will be NULL but the account_number will be populated.

### Import is slow
This is expected for large datasets. The import uses bulk operations and transactions, but processing millions of records takes time. You can monitor progress in the console output.

### Duplicate records
The import uses `ignore_conflicts=True`, so running the import multiple times won't create duplicates if there's a unique constraint. However, without explicit unique constraints, you may get duplicates on repeated imports. Clear the tables first if needed:

```python
from data.models import BuildingDetail, ExtraFeature
BuildingDetail.objects.all().delete()
ExtraFeature.objects.all().delete()
```

## Feature Code Reference

Common extra feature codes from HCAD:
- **POOL**: Swimming pool
- **POOLHTR**: Pool heater  
- **POOLCVR**: Pool cover/enclosure
- **SPA**: Spa/hot tub
- **DETGAR**: Detached garage
- **CARPORT**: Carport
- **GAZEBO**: Gazebo
- **PATIO**: Covered patio
- **SPRNK**: Sprinkler system
- **FENCE**: Fencing
- **TENNCT**: Tennis court
- **BTLFT**: Boat lift
- **DOCK**: Boat dock

See `downloads/Code_description_real/desc_r_10_extra_features.txt` for complete list with descriptions.
