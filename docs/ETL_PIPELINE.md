# ETL Pipeline Documentation

## Overview

The ETL (Extract, Transform, Load) pipeline provides automated data processing for Harris County Appraisal District (HCAD) property data. It downloads, extracts, transforms, and loads property records into the Django database.

## Quick Start

### Run Full Pipeline (with download)

```bash
docker compose exec web python manage.py etl_pipeline run
```

### Run with Pre-downloaded Data

```bash
docker compose exec web python manage.py etl_pipeline run --skip-download --skip-extract
```

## Architecture

```
data/etl_pipeline/
├── __init__.py          # Package exports
├── config.py            # Configuration management
├── download.py          # Download manager with retry logic
├── extract.py           # Archive extraction
├── transform.py         # Data parsing, validation, normalization
├── model_loader.py      # Django model loading
├── orchestrator.py      # Pipeline coordination
└── logging.py           # Structured logging
```

## Pipeline Stages

### 1. Download Stage

Downloads ZIP archives from HCAD's data portal:

- `Real_acct_owner.zip` - Property accounts and ownership
- `Real_building_land.zip` - Building details and extra features
- `Parcels.zip` - GIS parcel data (optional)

### 2. Extract Stage

Extracts downloaded ZIP archives to `downloads/` directory.

### 3. Transform Stage

Parses tab-delimited text files with schema-driven transformation:

| File | Schema | Target Model |
|------|--------|--------------|
| `real_acct.txt` | `real_acct` | PropertyRecord |
| `building_res.txt` | `building_res` | BuildingDetail |
| `extra_features.txt` | `extra_features` | ExtraFeature |

### 4. Load Stage

Uses `ModelLoader` to bulk insert records into Django models:

- **PropertyRecord**: Core property data (address, owner, value)
- **BuildingDetail**: Building characteristics (year built, sq ft, bedrooms)
- **ExtraFeature**: Additional features (pools, garages, patios)

## Data Models

### PropertyRecord

```python
PropertyRecord(
    account_number,    # HCAD 13-digit account ID
    address,           # Full site address
    city,
    zipcode,
    owner_name,
    value,             # Total appraised value
    assessed_value,
    building_area,
    land_area,
    latitude,          # GIS coordinates
    longitude,
)
```

### BuildingDetail

```python
BuildingDetail(
    property,          # FK to PropertyRecord
    account_number,
    building_number,
    year_built,
    heat_area,         # Living area in sq ft
    bedrooms,
    bathrooms,
    stories,
    quality_code,
    condition_code,
)
```

### ExtraFeature

```python
ExtraFeature(
    property,          # FK to PropertyRecord
    account_number,
    feature_code,      # Pool, garage, etc.
    feature_description,
    quantity,
    value,
)
```

## Command Options

```bash
python manage.py etl_pipeline <action> [options]

Actions:
  run          Run full ETL pipeline
  download     Download only
  extract      Extract only
  status       Show pipeline status

Options:
  --skip-download    Skip download stage
  --skip-extract     Skip extract stage
  --dry-run          Simulate without database changes
  --year YEAR        Data year (default: current year)
```

## Performance

Tested performance on real HCAD data:

| Stage | Records | Duration | Rate |
|-------|---------|----------|------|
| Property Records | 1,601,361 | ~5 min | ~5,300/sec |
| Building Details | 1,300,774 | ~4 min | ~5,000/sec |
| Extra Features | 1,158,687 | ~3 min | ~6,500/sec |
| **Total** | **4,060,822** | **~13 min** | **5,214/sec** |

Data quality: 99.99% success rate (269 invalid records out of 4M+)

## Configuration

Configuration is managed via environment variables:

```bash
# Download directory
HCAD_DOWNLOAD_DIR=/app/downloads

# Data year
HCAD_DATA_YEAR=2025

# Batch size for bulk inserts
ETL_BATCH_SIZE=5000

# Logging level
ETL_LOG_LEVEL=INFO
```

## Error Handling

- **Download retries**: 3 attempts with exponential backoff
- **Invalid records**: Logged and skipped (configurable)
- **Transaction safety**: Bulk inserts in atomic transactions
- **Progress logging**: Every 5,000 records

## Troubleshooting

### "No schema for file" warnings

Expected for lookup/description files (`desc_*.txt`). These are code tables, not data to import.

### Invalid records

Check logs for:
- Missing account numbers
- Account not found in PropertyRecord (must load PropertyRecord first)
- Data type conversion errors

### Memory issues

Reduce batch size:
```python
ModelLoader(config, batch_size=1000)
```

## Scheduled Tasks

The ETL pipeline is integrated with Celery for scheduled imports:

- **Monthly**: Building data import (2nd Tuesday at 2 AM)
- **Annual**: GIS data import (January 15 at 3 AM)

See `taxprotest/celery.py` for schedule configuration.

## Related Documentation

- [DATABASE.md](../DATABASE.md) - Data sources and schema
- [GIS.md](../GIS.md) - GIS features and location data
- [SETUP.md](../SETUP.md) - Installation and configuration
