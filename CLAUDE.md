# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Django web application for property tax protest analysis in Harris County, Texas. Uses HCAD data to search properties, find comparable properties via similarity scoring, and generate ARB hearing evidence reports with tax impact estimates.

---

## Architecture

| Layer | Location | Purpose |
|---|---|---|
| Django project | `taxprotest/` | Settings, URLs, Celery config, main views |
| Data app | `data/` | Models, ETL, similarity, admin, tasks |
| Templates | `templates/` | Bootstrap 5 HTML (all templates live here, not inside apps) |
| Scripts | `scripts/` | Entrypoint, build-time download, monitoring helpers |
| Docs | `docs/` | Reference documentation (see below) |

---

## Key Files

### Models (`data/models.py`)
- `PropertyRecord` — core property record; key flags: `is_residential`, `is_data_ready`
- `BuildingDetail` — building specs (sqft, bedrooms, bathrooms, quality, condition, etc.)
- `ExtraFeature` — pools, garages, patios, etc.
- `AssessmentHistory` — per-year assessed/appraised/market values with cap fields
- `TaxUnitRate` — annual adopted tax rate per taxing unit code
- `PropertyJurisdictionExemption` — per-account jurisdiction/exemption rows used for tax impact calculations
- `DownloadRecord` — tracks ETL download history

**Brazos CAD (PACS export)** — populated by `load_brazos_cad` via PostgreSQL COPY, not the ORM. Keyed by `prop_id` + `tax_year`; `prop_id`/`imp_id` are indexed integers rather than ForeignKeys, because PACS exports contain orphan rows that would abort a bulk load.
- `PropertyAccount` — account record; unique on (`prop_id`, `tax_year`)
- `PropertyLand` — land segments
- `PropertyImprovement` — improvements
- `PropertyImprovementDetail` — improvement details incl. sketch commands
- `PropertyEntity` — taxing jurisdiction codes
- `BrazosImportRun` — per-run audit trail (rows loaded/rejected, status)

### ETL & Analysis (`data/`)
- `etl.py` — shared ETL helpers (bulk upsert, data-ready marking)
- `brazos_layouts.py` — Brazos CAD fixed-width field offsets, types and implied decimal scales
- `brazos_copy.py` — shared PostgreSQL COPY plumbing for both Brazos loaders
- `comparables.py` — `ComparableProperty` + per-county sources; the seam that makes similarity county-neutral
- `residential.py` — `is_residential_state_class()`, `normalize_state_class()`
- `tasks_new.py` — Celery tasks: `download_and_import_building_data`, `download_and_import_gis_data`
- `similarity.py` — similarity scoring algorithm (see Similarity section below)
- `tax_impact.py` — `calculate_tax_impact(account_number, tax_year, median_assessed_value)` → `TaxImpactResult`; requires `TaxUnitRate` and `PropertyJurisdictionExemption` rows to be populated
- `assessment_history.py` — `evaluate_cap_status(entry, prior)` for cap analysis display
- `query.py` — `build_property_search_queryset(params)` for the main search view

### Management Commands (`data/management/commands/`)
| Command | Purpose |
|---|---|
| `import_all_data` | Authoritative full ETL — fails hard if completeness not achieved |
| `validate_data` | Enforces residential-only, data-ready contract |
| `reconcile_property_data` | Preview/apply cleanup of legacy mixed/incomplete rows (`--apply`) |
| `load_hcad_real_acct` | Property records only |
| `load_gis_data` | GIS coordinates from HCAD Parcels shapefile |
| `import_building_data` | Building details, features, room counts |
| `load_room_counts` | Room counts only (fixtures.txt) |
| `download_hcad` | Download HCAD source files |
| `import_jur_exemptions` | Upsert jurisdiction/exemption rows from TSV (`--path`, `--tax-year`) |
| `import_tax_unit_rates` | Upsert per-unit adopted tax rates from TSV (`--path`, `--tax-year`) |
| `load_brazos_cad` | Scrape, download and COPY-load the Brazos CAD certified roll (`--year`, `--list`, `--only`, `--dry-run`) |
| `load_brazos_gis` | Attach parcel coordinates to `PropertyAccount` from BCAD shapefiles (`--year`, `--list`, `--shapefile-year`, `--dry-run`) |

### Admin (`data/admin.py`)
Custom `DownloadRecordAdmin` with an ETL pipeline panel at `/admin/data/downloadrecord/`. Exposes:
- GIS import trigger button
- Building import trigger button
- Task status polling (async JSON endpoint)

### Views & URLs (`taxprotest/`)
**All property views are county-aware.** They take an optional `?county=hcad|brazos`; without it
the account is resolved by looking it up in each county in turn (HCAD first). The account/`prop_id`
in the URL is unchanged, so existing Harris links keep working.

| URL | View | Purpose |
|---|---|---|
| `/` | `index` | Property search, with a county filter (`?county=`) |
| `/similar/<account_number>/` | `similar_properties` | Comparable properties with protest recommendation |
| `/export/` | `export_csv` | CSV export of search results (County appended as the last column) |
| `/protest/<account_number>/` | `protest_analysis` | ARB hearing evidence report with equity analysis and tax impact |
| `/protest/<account_number>/export/` | `protest_analysis_export` | CSV export of protest comps + tax impact |
| `/protest/<account_number>/pdf/` | `protest_analysis_pdf` | PDF export of protest evidence report |

Views never touch a county's models directly. `_comparable_row()` flattens a `ComparableProperty`
into the single row shape every template renders, so a factor a district does not publish arrives
as `None` and shows as a dash instead of each county needing its own table. When adding a county,
the views and templates should need no changes at all.

Brazos specifics handled in the views: assessment history is assembled from its per-year
`PropertyAccount` rows (there is no `AssessmentHistory` table), and `_brazos_cap_status()` returns
the same dict shape as `evaluate_cap_status()` so the template's `.status`/`.label` lookups work.
Tax impact needs `TaxUnitRate`/`PropertyJurisdictionExemption`, which are Harris-only, so it
degrades to `completeness="missing"` with an explanatory note rather than a blank panel.
| `/about/` | `about` | About page |
| `/healthz/` | `healthz` | Liveness probe |
| `/readiness/` | `readiness` | Readiness probe |
| `/admin/` | Django admin | Admin interface |

---

## Similarity Algorithm

`data/similarity.py` — `find_similar_properties(account_number, max_distance_miles=10.0, max_results=50, min_score=30.0, source=None)` → `List[Dict]`

**County-neutral.** The algorithm scores `ComparableProperty` objects from `data/comparables.py`,
which is where each district's models are mapped onto one shape. `source` selects the county
(`"hcad"` or `"brazos"`); omitted, the key is looked up in each in turn, HCAD first. **Adding a
district means writing a source in `comparables.py`, not editing `similarity.py`.**

| Source | Models | Key |
|---|---|---|
| `HcadSource` | `PropertyRecord` + `BuildingDetail` + `ExtraFeature` | `account_number` |
| `BrazosSource` | `PropertyAccount` + `PropertyImprovementDetail` | `prop_id` (latest year unless pinned) |

Brazos has no single "building" row, so `comparables.py` folds improvement details into
building-level facts: living area is the sum of `MA*` rows (main area, second floor), the other
detail codes (`AG`, `OP`, `SP`…) become features, and quality/year come from the largest dwelling
row. A `MA2` row implies two storeys — the only storey signal Brazos publishes.

**Missing factors are skipped, never zeroed.** Brazos publishes no bedrooms, bathrooms or
condition, so those arrive as `None` and `_score_from_components` renormalises over the rest, with
the usual completeness penalty. Rankings stay sound because every candidate in one search shares
the same gaps — but an absolute score is only comparable *within* a county, not across them.

Quality codes differ by district: HCAD grades with letters (`A`/`B`/`C`), Brazos embeds a digit in
a structured code (`RV3`, `RV4P`). `_quality_similarity` tries the letter rank, then an ordinal
grade extracted from the digits, then falls back to categorical matching.

Result dicts keep `property` (the underlying model instance), `building` and `features` (the
county's own row types); `comparable` and `source` are added alongside. Comparables are always
drawn from the target's own county — the search never mixes districts.

**Distance** is a filter only — candidates beyond `max_distance_miles` are excluded before scoring. Distance does not affect the score.

**Residential weights** (`RESIDENTIAL_WEIGHTS`):

| Factor | Weight |
|---|---|
| Living area | 24% |
| Bedrooms | 14% |
| Bathrooms | 12% |
| Land size | 10% |
| Quality | 10% |
| Age | 8% |
| Condition | 6% |
| Stories | 4% |
| Building character | 4% |
| Extra features | 4% |

**Land-only** properties use `LAND_ONLY_WEIGHTS` (land_size 80%, features 10%, distance 10%).

**Score labels:** Excellent (≥84) · Good (≥70) · Fair (≥52) · Partial (≥36) · Poor (<36)

---

## Developer Workflows

**Always use Docker Compose.** Never run Django or Celery directly with local Python.

```bash
# Start everything
docker compose up --build

# Start without Celery (faster for UI work)
docker compose up web db redis

# Run full import
docker compose exec web python manage.py import_all_data

# Validate imported data
docker compose exec web python manage.py validate_data

# Brazos CAD certified roll (latest year; ~1.2M rows, a few minutes)
# Staged .zip + extracts (~1.6 GB/year) are deleted automatically on success.
docker compose exec web python manage.py load_brazos_cad
docker compose exec web python manage.py load_brazos_cad --list
docker compose exec web python manage.py load_brazos_cad --year 2024 --only PropertyAccount
docker compose exec web python manage.py load_brazos_cad --keep-archive   # skip re-download next run

# Brazos CAD parcel coordinates (run after load_brazos_cad for the same year)
docker compose exec web python manage.py load_brazos_gis --year 2025
docker compose exec web python manage.py load_brazos_gis --list

# Reclaim ETL staging disk (run from the host, not the container)
./scripts/cleanup_data.sh --dry-run          # always check first
./scripts/cleanup_data.sh                    # extracted/derived files only
./scripts/cleanup_data.sh --brazos --all     # one pipeline's staging
./scripts/cleanup_data.sh --all --yes        # everything, no prompt

# Shell
docker compose exec web python manage.py shell

# Migrations
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py migrate

# Create admin user
docker compose exec web python manage.py createsuperuser

# Logs
docker compose logs -f web
docker compose logs -f worker
docker compose logs -f beat
```

---

## Running Tests

```bash
# Full suite
docker compose exec web python manage.py test

# Single test module
docker compose exec web python manage.py test data.tests.test_tax_impact

# Single test case or method
docker compose exec web python manage.py test data.tests.test_similarity_scoring.SimilarityScoringTest.test_score_label
```

Test files live in `data/tests/`:
- `test_admin.py` — admin views and ETL trigger endpoints
- `test_assessment_history.py` — cap status evaluation
- `test_bedroom_bathroom_data.py` — room count data validation
- `test_data_integrity.py` — data integrity checks
- `test_load_gis_data.py` — GIS import command
- `test_residential_etl.py` — residential classification and ETL helpers
- `test_runtime_paths.py` — runtime path resolution
- `test_similarity_scoring.py` — similarity score calculations
- `test_tasks_new.py` — Celery task logic
- `test_tax_impact.py` — `calculate_tax_impact()` logic
- `test_tax_import_commands.py` — `import_jur_exemptions` and `import_tax_unit_rates` commands
- `test_brazos_layouts.py` — BCAD fixed-width field offsets, value conversion, export header
- `test_load_brazos_cad.py` — BCAD record reader, COPY encoding, upsert SQL, staging cleanup
- `test_brazos_models.py` — BCAD value accessors, situs address, import-run provenance
- `test_load_brazos_gis.py` — BCAD parcel layer/column selection, coordinate application
- `test_comparables.py` — county mapping, source resolution, cross-county scoring

View tests live in `taxprotest/tests/`:
- `test_views.py` — Harris behaviour (search, export, similarity, protest analysis)
- `test_views_multicounty.py` — Brazos rendering, county resolution and filtering, history and
  cap status from account rows, tax-impact degradation

---

## Code Quality

Pre-commit hooks (Black, Ruff, EOF/whitespace fixers). Install once:

```bash
pip install -r requirements.txt
pre-commit install
```

Run manually:

```bash
pre-commit run --all-files
mypy
```

---

## Templates

All templates live in the top-level `templates/` directory — not inside any app.

```
templates/
├── base.html                  # Bootstrap 5 layout, navbar, footer
├── index.html                 # Property search page
├── similar_properties.html    # Comparable properties view
├── protest_analysis.html      # ARB evidence report (equity + tax impact)
├── about.html                 # About page
├── includes/
│   ├── navbar.html
│   └── footer.html
├── components/
│   ├── index.html
│   └── sort_header.html
└── admin/
    └── data/
        └── downloadrecord/    # Custom admin ETL pipeline templates
```

New templates must extend `base.html`. Use Bootstrap 5 for all UI.

---

## Background Tasks (Celery)

Configured in `taxprotest/celery.py`. Redis is the broker.

| Task | Schedule | Function |
|---|---|---|
| Building data import | 2nd Tuesday of month, 2 AM Central | `tasks_new.download_and_import_building_data` |
| GIS import | January 15, 3 AM Central | `tasks_new.download_and_import_gis_data` |

To add a new scheduled task, update `beat_schedule` in `taxprotest/celery.py`.

---

## Static Files

`staticfiles/` is **not committed to git** — it is generated at container build time by `collectstatic` (Dockerfile line 28). Do not add it back to version control.

---

## Data Sources

HCAD: https://download.hcad.org/data/

| File | Contents |
|---|---|
| `Real_acct_owner.txt` | Property records |
| `Real_building_land.zip` | Building details and features |
| `Parcels.zip` | GIS shapefiles (~800MB) |

Downloaded at build time via `scripts/build_time_download.py`. Re-download targets live in `var/downloads/`, with extracted data under `var/extracted/`.

Brazos CAD: https://brazoscad.org/certified-data-downloads/

One certified-roll `.zip` per tax year, containing fixed-width PACS (Harris Govern / True Automation) `.TXT` files. Loaded by `load_brazos_cad`; byte offsets live in `data/brazos_layouts.py`.

| File | Model | Contents |
|---|---|---|
| `APPRAISAL_INFO.TXT` | `PropertyAccount` | Property/account records (~1.4 GB, 9247-char records) |
| `APPRAISAL_LAND_DETAIL.TXT` | `PropertyLand` | Land segments |
| `APPRAISAL_IMPROVEMENT_INFO.TXT` | `PropertyImprovement` | Improvements |
| `APPRAISAL_IMPROVEMENT_DETAIL.TXT` | `PropertyImprovementDetail` | Improvement details + sketch commands |
| `APPRAISAL_ENTITY.TXT` | `PropertyEntity` | Taxing jurisdiction codes |

Downloads and extracted files stage in `data/cad_downloads/` (bind-mounted, gitignored). A year is ~1.6 GB staged; `load_brazos_cad` deletes it after a successful load, and `scripts/cleanup_data.sh` reclaims leftovers from interrupted runs.

**Never read `appraised_val` / `assessed_val` directly.** Despite their names, both are computed
*before* the agricultural productivity deduction, so on ag land they overstate value — often
tenfold. Use the `PropertyAccount` accessors, which resolve to the figures the district publishes:

| Use this | Backed by | esearch label |
|---|---|---|
| `.market_value` | `appraised_val` | Market Value |
| `.appraised_value` | `appraised_val_prod_loss` | Appraised Value |
| `.assessed_value` | `assessed_val_prod_loss` | Assessed Value |
| `.ag_value_loss` | ag/timber market minus use value | Ag Value Loss |
| `.situs_address` | `situs_num` + prefix/street/suffix/unit | Situs Address |
| `.taxing_units` | `entities`, split on commas | — |
| `.has_location` | `latitude`/`longitude` | — |

Other fields worth knowing: `circuit_breaker_val` (SB2 2023 limitation, non-zero on 4.7% of 2025
rows), `entities` (per-property taxing units, populated on 100% of rows — `PropertyEntity` is only
a code lookup), and `dataset_id` (joins a row to its `BrazosImportRun`).

**Layout caveats.** `situs_num` sits at offset 4460, ~3.3 KB from the rest of the address — easy to
miss. `circuit_breaker_val` (9068–9082) is *past* the published 9067-character layout: Brazos emits
9247-char records, but the 2021 export is exactly 9067. It is therefore marked `optional=True` in
`brazos_layouts.py` and excluded from `min_width`, so older exports still load with the column NULL
instead of being rejected wholesale. Mark any other post-9067 field the same way.

**Data vintage.** `BrazosImportRun` records the export header (run date, supplement number, dataset
id, PACS version); `run.is_certified_roll` is True only when supplement is 0. The certified roll is
a snapshot — esearch reflects post-certification supplements, so live values drift from it over
time. That drift is expected and is not an ETL fault.

Verified against esearch.brazoscad.org with `scripts/verify_brazos_against_esearch.py`
(17 fields/property; 0 unexplained mismatches).

**Parcel geometry.** The certified roll has no spatial data; coordinates come from a separate
shapefile via `load_brazos_gis`, from https://brazoscad.org/tax-information/gis/. Run it *after*
`load_brazos_cad` for the same year.

- Joins on `PROP_ID`. Coverage is **94.5% of real property** (`prop_type_cd='R'`); mineral,
  personal-property and mobile-home accounts have no parcel boundary and stay NULL by nature, so
  the ~50% figure across all accounts is expected, not a failure.
- Source CRS is NAD83 Texas Central (ftUS), reprojected to EPSG:4326. `parcel_area_sqft` is
  computed *before* reprojection, and cross-checks against `land_acres` from the roll to ~1%.
- Uses `representative_point()`, never `centroid` — a centroid falls outside concave river-front
  and cul-de-sac parcels, which would attribute a point to a neighbouring property.
- **Archive layout varies wildly by year.** 2025 ships a single `Parcels_*.shp`; 2024 ships a
  33-layer GIS package where `Parcel_ID.shp` is map lettering and the boundaries live in
  `Public_Parcel_Boundary_certified.shp`. The loader picks the largest polygon layer carrying a
  property-id column, then chooses among id columns by which yields the most usable ids — 2024 has
  `PROP_ID` holding `'R22549'` and `PROP_ID1` holding `22549`. Do not hardcode either.

---

## Documentation

| File | Contents |
|---|---|
| `TODO.md` | Outstanding work, known gaps and caveats — **read at the start of a session, update in the same commit as the work** |
| `README.md` | Overview, features, quick start, similarity weights |
| `DEPLOYMENT.md` | CD pipeline, SSH keys, Docker permissions, GitHub secrets |
| `docs/guides/SETUP.md` | Installation, Docker services, production deployment |
| `docs/guides/DATABASE.md` | ETL processes, import commands, DB management |
| `docs/guides/GIS.md` | GIS data handling, coordinate storage, similarity distance |
| `docs/SIMILARITY_SCORING.md` | Similarity algorithm deep-dive |
| `docs/ETL_PIPELINE.md` | ETL pipeline architecture |
| `docs/REVERSE_PROXY.md` | Reverse proxy / production deployment notes |

---

## Conventions

- Add new views to `taxprotest/views.py` (or a new app's `views.py`) and register in `taxprotest/urls.py`
- Use environment variables for all secrets/configuration — never hardcode
- `is_residential=True` and `is_data_ready=True` are the contract for queryable properties
- All ETL helper logic goes in `data/etl.py` or `data/residential.py`, not inline in management commands
- Celery tasks import from `data.tasks_new` — `data.tasks` (if it exists) is legacy
- Tax impact calculations (`data/tax_impact.py`) require `TaxUnitRate` and `PropertyJurisdictionExemption` rows to be populated via `import_tax_unit_rates` and `import_jur_exemptions` before the protest analysis views will show meaningful results; missing data degrades gracefully to `completeness="missing"`
