# Database Configuration, Schema & ETL Guide

Comprehensive guide to database management, multi-county schemas, data imports, and ETL processes for TaxProtest-Django.

---

## Table of Contents

- [1. Architecture Overview](#1-architecture-overview)
- [2. Database Schemas](#2-database-schemas)
  - [Harris County (HCAD)](#harris-county-hcad)
  - [Brazos County (BCAD)](#brazos-county-bcad)
- [3. Data Sources](#3-data-sources)
- [4. Import Workflows](#4-import-workflows)
  - [Harris County Production Path](#harris-county-production-path)
  - [Brazos County Production Path](#brazos-county-production-path)
  - [Tax Impact & Assessment History Ingestion](#tax-impact--assessment-history-ingestion)
- [5. ETL Pipeline Architecture](#5-etl-pipeline-architecture)
  - [Streaming Row Reader](#streaming-row-reader)
  - [PostgreSQL COPY Fast Loader](#postgresql-copy-fast-loader)
  - [GIS Centroid Loader](#gis-centroid-loader)
  - [Extra Feature Detail Ingestion](#extra-feature-detail-ingestion)
- [6. Scheduled Background Imports (Celery Beat)](#6-scheduled-background-imports-celery-beat)
- [7. Verification & Diagnostic Commands](#7-verification--diagnostic-commands)

---

## 1. Architecture Overview

TaxProtest-Django supports multiple Texas appraisal districts. Each county brings its own distinct data schemas, source file layouts, and ETL pipelines, but maps into a shared web interface ([`counties/common/`](file:///home/specter/dev/TaxProtest-Django/counties/common)):

```
counties/
├── harris/               # App label: 'data' (table prefix: data_*)
│   ├── models.py         # HCAD PropertyRecord, BuildingDetail, ExtraFeature, AssessmentHistory
│   ├── etl_pipeline/     # RowReader, FastLoader (COPY), GISLoader, Readiness
│   ├── similarity.py     # Harris similarity scoring implementation
│   └── adapter.py        # Neutral Subject/Comp adapter
├── brazos/               # App label: 'brazos_cad' (table prefix: brazos_cad_*)
│   ├── models.py         # PACS PropertyAccount, PropertyImprovement, PropertyLand, etc.
│   ├── parsers/          # PACS fixed-width & tab-delimited text parsers
│   ├── similarity.py     # Brazos similarity scoring implementation
│   └── adapter.py        # Neutral Subject/Comp adapter
└── common/               # Shared contracts, similarity math, analysis, charts, exports
```

---

## 2. Database Schemas

### Harris County (HCAD)

Database tables use the `data_` prefix (app label `data`):

#### `PropertyRecord` (`data_propertyrecord`)
- `account_number` (PK, `CharField(13)`): 13-digit HCAD account identifier.
- `owner_name`, `address`, `city`, `zipcode`, `street_number`, `street_name`.
- `assessed_value`, `building_area`, `land_area`.
- `state_class`: Property use classification (e.g. `A1` single-family residential).
- `is_residential` (`BooleanField`): Filtered flag derived from residential state classes.
- `is_data_ready` (`BooleanField`): `True` when residential building, room-count, and GIS coordinates are fully populated.
- `latitude`, `longitude` (`FloatField`): GIS coordinates calculated from parcel centroids.
- `parcel_id` (`CharField(30)`): GIS parcel identifier.

#### `BuildingDetail` (`data_buildingdetail`)
- `property` (`ForeignKey(PropertyRecord)`): Associated property.
- `account_number`, `building_number`: Compound identifier.
- `building_type`, `building_style`, `building_class`.
- `quality_code`: Ranked rating (`X`, `A`, `B`, `C`, `D`, `E`, `F`).
- `condition_code`: Physical condition code.
- `year_built`, `year_remodeled`, `effective_year`.
- `heat_area`: Heated living area (sq ft).
- `bedrooms`, `bathrooms`, `half_baths`, `fireplaces`.
- `is_active` (`BooleanField`): Active record flag.
- `import_date`, `import_batch_id`: Import metadata.

#### `ExtraFeature` (`data_extrafeature`)
- `property` (`ForeignKey(PropertyRecord)`).
- `account_number`, `feature_code`, `feature_description`.
- `quantity`, `length`, `width`, `quality_code`, `condition_code`, `year_built`, `value`.
- `is_active`, `import_date`, `import_batch_id`.

#### `AssessmentHistory` (`data_assessmenthistory`)
- `account_number`, `year`, `county` (`harris`).
- `market_value`, `appraised_value`, `assessed_value`.
- `homestead_cap_loss`, `homestead_percent`: 10% annual homestead cap tracking.

#### `TaxUnitRate` & `PropertyJurisdictionExemption`
- Taxing entity tax rates and property-specific jurisdiction exemptions for tax impact calculations.

---

### Brazos County (BCAD)

Database tables use the `brazos_cad_` prefix (app label `brazos_cad`):

#### `PropertyAccount` (`brazos_cad_propertyaccount`)
- `prop_id` (PK, `IntegerField`): BCAD property identifier.
- `geo_id` (`CharField(50)`): Geographic parcel ID.
- `owner_name`, `situs_street_pre`, `situs_street`, `situs_city`, `situs_zip`.
- `market_value`, `appraised_value`, `assessed_value`, `land_acres`.
- `class_code`, `is_residential`, `latitude`, `longitude`.

#### `PropertyImprovement` & `PropertyBuildingCharacteristic`
- Improvement type, state class, year built, living area, quality, condition, bedrooms, bathrooms, and second-floor flags.

#### `PropertyExtraFeature` (`brazos_cad_propertyextrafeature`)
- Additional structural amenities (garages, sheds, pools, porches).

#### `PropertyEntity` & `BrazosAssessmentHistory`
- Multi-year certified assessment history (2016–2025) and taxing jurisdiction tax rates.

---

## 3. Data Sources

### Harris County (HCAD)
- **Portal:** `https://download.hcad.org/data/` & `https://download.hcad.org/GIS/`
- `Real_acct_owner.zip`: Accounts, owners, values (`real_acct.txt`).
- `Real_building_land.zip`: Building characteristics (`building_res.txt`), extra features (`extra_features_detail1.txt`, `extra_features_detail2.txt`, `extra_features.txt`), room counts (`fixtures.txt`), land specs (`land.txt`).
- `Real_jur_exempt.zip`: Tax rates and exemptions (`jur_tax_dist_exempt_value_rate.txt`, `jur_exempt.txt`).
- `Parcels.zip`: GIS parcel boundary shapefiles (`ParcelsCity.shp`).

### Brazos County (BCAD)
- **Portal:** BCAD certified data downloads (`pacs_certified_data_*.zip`).
- PACS fixed-width / tab-delimited files: `PROP.TXT`, `IMPRV.TXT`, `IMPRV_DET.TXT`, `LAND.TXT`, `APPRAISAL_ENTITY_INFO.TXT`.
- GIS Parcel Shapefiles: `Parcels.shp`.

---

## 4. Import Workflows

### Harris County Production Path

The authoritative production ETL flow uses `import_all_data`, enforcing residential completeness before finishing:

```bash
# 1. Authoritative full import (downloads, extracts, parses, loads COPY, links GIS)
docker compose exec web python manage.py import_all_data

# 2. Strict dataset validation check
docker compose exec web python manage.py validate_data

# 3. Optional reconciliation check for legacy databases
docker compose exec web python manage.py reconcile_property_data --apply
```

#### Individual Stage Commands (Harris)

When updating specific slices:

```bash
# Load property records only
docker compose exec web python manage.py load_hcad_real_acct

# Load GIS coordinates only
docker compose exec web python manage.py load_gis_data --skip-download

# Load building specs, room counts, and extra features
docker compose exec web python manage.py import_building_data

# Ingest tax jurisdictions and exemption rates
docker compose exec web python manage.py import_hcad_jur_exempt --tax-year 2025

# Ingest multi-year historical assessment data
docker compose exec web python manage.py import_assessment_history
```

---

### Brazos County Production Path

```bash
# 1. Load Brazos PACS appraisal data
docker compose exec web python manage.py load_brazos_cad

# 2. Ingest Brazos GIS parcel shapefiles
docker compose exec web python manage.py load_brazos_gis

# 3. Ingest current tax unit rates
docker compose exec web python manage.py import_brazos_tax_rates

# 4. Ingest multi-year certified assessment history (2021-2025)
docker compose exec web python manage.py import_brazos_assessment_history --start-year 2021 --end-year 2025

# 5. Validate ingested database against source files
docker compose exec web python manage.py validate_brazos_against_source
```

---

## 5. ETL Pipeline Architecture

The modern Harris ETL uses a single-path, high-performance architecture:

```
Source Files (.txt / .shp)
          │
          ▼
┌──────────────────┐
│  row_reader.py   │  Single source of truth for column mappings,
└─────────┬────────┘  type coercion, and residential filters.
          │ (RowResult stream)
          ▼
┌──────────────────┐
│  fast_loader.py  │  PostgreSQL staging table + COPY format CSV +
└─────────┬────────┘  INSERT ON CONFLICT DO NOTHING (batch size: 5,000-10,000)
          │
          ▼
┌──────────────────┐
│  readiness.py    │  Computes and indexes PropertyRecord.is_data_ready
└──────────────────┘
```

### PostgreSQL COPY Fast Loader
- Creates a temporary staging table (`CREATE TEMP TABLE ... AS SELECT ... LIMIT 0`).
- Streams CSV formatted rows using `cursor.copy_expert("COPY staging_table FROM STDIN WITH (FORMAT CSV)")`.
- Performs a set-based `INSERT INTO target_table SELECT * FROM staging_table ON CONFLICT (key) DO NOTHING`.
- Eliminates duplicate key aborts and loads 4M+ records in under 12 minutes.

### Extra Feature Detail Ingestion
Detail files (`extra_features_detail1.txt`, `extra_features_detail2.txt`) take priority to capture human-readable descriptions (`Gunite Pool`, `Frame Detached Garage`, etc.), physical dimensions (`length`, `width`), and appraised feature values.

---

## 6. Scheduled Background Imports (Celery Beat)

Configured in [`taxprotest/celery.py`](file:///home/specter/dev/TaxProtest-Django/taxprotest/celery.py):

| Task Name | Schedule | Target Scope |
|---|---|---|
| `download-and-import-building-data-monthly` | 2nd Tuesday of month @ 2:00 AM Central | `run_etl_pipeline(scope="building-only", strict=True)` |
| `download-and-import-gis-data-annually` | January 15th @ 3:00 AM Central | `run_etl_pipeline(scope="gis-only", strict=True)` |

Monitor worker and beat logs:
```bash
docker compose logs -f beat
docker compose logs -f worker
```

---

## 7. Verification & Diagnostic Commands

### Query Database Health & Readiness

```bash
docker compose exec web python manage.py shell -c "
from counties.harris.models import PropertyRecord, BuildingDetail, ExtraFeature
from counties.brazos.models import PropertyAccount

print('=== HARRIS COUNTY ===')
print(f'Total Properties:     {PropertyRecord.objects.count():,}')
print(f'Residential:          {PropertyRecord.objects.filter(is_residential=True).count():,}')
print(f'Data-Ready (Queryable):{PropertyRecord.objects.filter(is_data_ready=True).count():,}')
print(f'Active Buildings:     {BuildingDetail.objects.filter(is_active=True).count():,}')
print(f'Active Features:      {ExtraFeature.objects.filter(is_active=True).count():,}')

print('\n=== BRAZOS COUNTY ===')
print(f'Total Accounts:       {PropertyAccount.objects.count():,}')
print(f'Residential Accounts: {PropertyAccount.objects.filter(is_residential=True).count():,}')
print(f'With GIS Coordinates: {PropertyAccount.objects.filter(latitude__isnull=False).count():,}')
"
```

### Validate Data Ready Integrity

```bash
docker compose exec web python manage.py validate_data
```
