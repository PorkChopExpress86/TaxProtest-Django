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

### ETL & Analysis (`data/`)
- `etl_pipeline/` — **the modern, authoritative ETL package.** `ETLConfig.from_env()` builds config; `ETLOrchestrator.execute(scope=..., strict=...)` runs the download → extract → transform → load → contract-validation pipeline. Composed of `config.py`, `download.py`, `extract.py`, `transform.py`, `model_loader.py`, `fast_loader.py` (COPY-based bulk load), `orchestrator.py`, `logging.py`. Both `import_all_data` and the Celery `run_etl_pipeline` task delegate here.
- `etl.py` — lower-level shared helpers (`bulk_load_properties`, `load_building_details`, `load_extra_features`, `load_fixtures_room_counts`, `load_gis_parcels`, `link_orphaned_records`, `refresh_property_readiness`) used by the granular `load_*` management commands. Not legacy; complements `etl_pipeline/`.
- `residential.py` — `is_residential_state_class()`, `normalize_state_class()`
- `tasks_new.py` — Celery tasks. Scheduled work routes through `run_etl_pipeline(scope=...)`; `download_and_import_building_data` / `download_and_import_gis_data` remain as thin task wrappers.
- `similarity.py` — similarity scoring algorithm (see Similarity section below)
- `tax_impact.py` — `calculate_tax_impact(account_number, tax_year, median_assessed_value)` → `TaxImpactResult`; requires `TaxUnitRate` and `PropertyJurisdictionExemption` rows to be populated
- `assessment_history.py` — `evaluate_cap_status(entry, prior)` for cap analysis display
- `query.py` — `build_property_search_queryset(params)` for the main search view

Project-level (`taxprotest/`): `forms.py` (`ContactForm`), `middleware.py` (`RequestLoggingMiddleware`, registered in settings), `runtime_paths.py` (`RuntimePaths` — resolves `var/` download/extract/log/report dirs from env, used by the ETL pipeline).

### Management Commands (`data/management/commands/`)
| Command | Purpose |
|---|---|
| `import_all_data` | Authoritative full ETL via `ETLOrchestrator` — fails hard if completeness not achieved |
| `etl_pipeline` | Low-level pipeline driver with subcommands: `run` (`--scope`, `--skip-*`, `--dry-run`, `--allow-partial`), `download`, `extract`, `status`, `cleanup`, `list` |
| `check_and_import_data` | Conditional import — only runs ETL if data is missing/stale (used by the `refresh` job service) |
| `validate_data` | Enforces residential-only, data-ready contract |
| `reconcile_property_data` | Preview/apply cleanup of legacy mixed/incomplete rows (`--apply`) |
| `load_hcad_real_acct` | Property records only |
| `load_gis_data` | GIS coordinates from HCAD Parcels shapefile |
| `import_building_data` | Building details, features, room counts |
| `load_building_features` | Building details + extra features only |
| `load_room_counts` | Room counts only (fixtures.txt) |
| `import_assessment_history` | Per-year assessment/cap history rows |
| `link_orphaned_records` | Link building/feature rows to their parent property records |
| `download_hcad` | Download HCAD source files |
| `import_jur_exemptions` | Upsert jurisdiction/exemption rows from TSV (`--path`, `--tax-year`) |
| `import_tax_unit_rates` | Upsert per-unit adopted tax rates from TSV (`--path`, `--tax-year`) |

### Admin (`data/admin.py`)
Branded via `admin.site.site_header`/`site_title`/`index_title` ("Home Values Admin"). Custom
`DownloadRecordAdmin` with an ETL pipeline panel at `/admin/data/downloadrecord/`. Exposes:
- GIS import trigger button
- Building import trigger button
- Task status polling (async JSON endpoint)

All models are registered: `PropertyRecord`, `BuildingDetail`, `ExtraFeature`, `AssessmentHistory`
(list/filter/search wired up; large tables get `show_full_result_count = False` to skip a slow
`COUNT(*)` at HCAD scale — see docs/guides/DATABASE.md row counts). `AssessmentHistory`,
`TaxUnitRate`, and `PropertyJurisdictionExemption` are read-only in the admin
(`ReadOnlyImportedDataAdminMixin` blocks add/change/delete) since they're only ever populated by
their respective `import_*` management commands — corrections must go through re-running the
import, not hand-editing. Custom branding/styling lives in `templates/admin/base_site.html` +
`data/static/admin/css/custom_admin.css` (dark-mode-aware via Django's admin CSS custom
properties).

### Views & URLs (`taxprotest/`)
| URL | View | Purpose |
|---|---|---|
| `/` | `index` | Property search |
| `/similar/<account_number>/` | `similar_properties` | Comparable properties with protest recommendation |
| `/export/` | `export_csv` | CSV export of search results |
| `/protest/<account_number>/` | `protest_analysis` | ARB hearing evidence report with equity analysis and tax impact |
| `/protest/<account_number>/export/` | `protest_analysis_export` | CSV export of protest comps + tax impact |
| `/protest/<account_number>/pdf/` | `protest_analysis_pdf` | PDF export of protest evidence report |
| `/about/` | `about` | About page |
| `/contact/` | `contact` | Contact form (`ContactForm`) |
| `/healthz/` | `healthz` | Liveness probe |
| `/readiness/` | `readiness` | Readiness probe |
| `/admin/` | Django admin | Admin interface |

---

## Similarity Algorithm

`data/similarity.py` — `find_similar_properties(account_number, max_distance_miles=10.0, max_results=50, min_score=30.0)` → `List[Dict]`

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

**Always use Docker Compose.** Never run Django or Celery directly with local Python. The `Makefile` wraps the common flows — prefer it.

**Services:** `postgres`, `redis`, `migrate` (one-shot migrator), `web` (production-style app), `worker`, `beat`, `taxprotest-dev` (dev image with lint/test tooling, profile `devtools`), `ingest` + `refresh` (one-shot job services, profile `jobs`). Dev tooling deps (pytest, ruff, black, mypy) live **only** in the `taxprotest-dev` image.

```bash
# Build images (skips the slow HCAD data download)
make build

# Start postgres + production app (no Celery)
make up

# Start postgres + dev app (devtools profile)
make dev

# Run a Django mgmt command in the dev container
docker compose run --rm taxprotest-dev python manage.py validate_data
docker compose run --rm taxprotest-dev python manage.py shell
docker compose run --rm taxprotest-dev python manage.py migrate

# Full import / refresh (jobs profile)
make ingest      # runs: import_all_data
make refresh     # runs: check_and_import_data

# Shell + logs
make shell
make logs        # follows the dev app; use `docker compose logs -f worker|beat` for Celery
```

---

## Running Tests

Tests are Django `TestCase`-based and run with pytest (configured in `pytest.ini`). Because test deps live only in the dev image, run them through `taxprotest-dev` — `make test` is the canonical path.

```bash
# Full suite
make test                                              # docker compose run --rm taxprotest-dev pytest -q

# Single module / case / method
docker compose run --rm taxprotest-dev pytest data/tests/test_tax_impact.py
docker compose run --rm taxprotest-dev pytest data/tests/test_similarity_scoring.py::SimilarityScoringTests

# Lint, format, type-check (all in the dev container)
make lint        # ruff check + black --check
make fmt         # ruff --fix + black
make type        # mypy taxprotest data
```

Test files live in three locations: `data/tests/` (app logic), `data/etl_pipeline/tests/` (pipeline modules), and `tests/scripts/` (standalone script tests). In `data/tests/`:
- `test_admin.py` — admin views and ETL trigger endpoints
- `test_assessment_history.py` — cap status evaluation
- `test_bedroom_bathroom_data.py` — room count data validation
- `test_data_integrity.py` — data integrity checks
- `test_fast_loader.py` — COPY-based bulk loader
- `test_load_gis_data.py` — GIS import command
- `test_residential_etl.py` — residential classification and ETL helpers
- `test_runtime_paths.py` — runtime path resolution
- `test_similarity_scoring.py` — similarity score calculations
- `test_tasks_new.py` — Celery task logic
- `test_tax_impact.py` — `calculate_tax_impact()` logic
- `test_tax_import_commands.py` — `import_jur_exemptions` and `import_tax_unit_rates` commands

View tests live in `taxprotest/tests/`.

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
├── contact.html               # Contact form page
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

Configured in `taxprotest/celery.py` (timezone `America/Chicago`). Redis is the broker. Both scheduled jobs call the same task, `data.tasks_new.run_etl_pipeline`, with a different `scope` kwarg.

| Task | Schedule | Task / kwargs |
|---|---|---|
| Building data import | 2nd Tuesday of month, 2 AM Central | `run_etl_pipeline(scope="building-only", strict=True)` |
| GIS import | January 15, 3 AM Central | `run_etl_pipeline(scope="gis-only", strict=True)` |

To add a new scheduled task, update `beat_schedule` in `taxprotest/celery.py`.

---

## Static Files

`staticfiles/` is **not committed to git** — it is generated at container build time by `collectstatic` (in the Dockerfile). Do not add it back to version control.

---

## Data Sources

HCAD: https://download.hcad.org/data/

| File | Contents |
|---|---|
| `Real_acct_owner.txt` | Property records |
| `Real_building_land.zip` | Building details and features |
| `Parcels.zip` | GIS shapefiles (~800MB) |

Downloaded at build time via `scripts/build_time_download.py`. Re-download targets live in `var/downloads/`, with extracted data under `var/extracted/`.

---

## Documentation

| File | Contents |
|---|---|
| `README.md` | Overview, features, quick start, similarity weights |
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
