# CLAUDE.md — TaxProtest-Django

Django web application for property tax protest analysis in Harris County, Texas. Uses HCAD data to search properties, find comparable properties via similarity scoring, and display building/feature details.

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
- `DownloadRecord` — tracks ETL download history

### ETL (`data/`)
- `etl.py` — shared ETL helpers (bulk upsert, data-ready marking)
- `residential.py` — `is_residential_state_class()`, `normalize_state_class()`
- `tasks_new.py` — Celery tasks: `download_and_import_building_data`, `download_and_import_gis_data`
- `similarity.py` — similarity scoring algorithm (see Similarity section below)

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

### Admin (`data/admin.py`)
Custom `DownloadRecordAdmin` with an ETL pipeline panel at `/admin/data/downloadrecord/`. Exposes:
- GIS import trigger button
- Building import trigger button
- Task status polling (async JSON endpoint)

### Views & URLs (`taxprotest/`)
| URL | View | Purpose |
|---|---|---|
| `/` | `index` | Property search |
| `/similar/<account_number>/` | `similar_properties` | Comparable properties |
| `/export/` | `export_csv` | CSV export of search results |
| `/about/` | `about` | About page |
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
docker compose exec web python manage.py test
```

Test files live in `data/tests/`:
- `test_admin.py` — admin views and ETL trigger endpoints
- `test_load_gis_data.py` — GIS import command
- `test_residential_etl.py` — residential classification and ETL helpers
- `test_similarity_scoring.py` — similarity score calculations
- `test_tasks_new.py` — Celery task logic
- `test_data_integrity.py` — data integrity checks
- `test_bedroom_bathroom_data.py` — room count data validation

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

Downloaded at build time via `scripts/build_time_download.py`. Re-download targets live in `downloads/`.

---

## Documentation

| File | Contents |
|---|---|
| `README.md` | Overview, features, quick start, similarity weights |
| `SETUP.md` | Installation, Docker services, production deployment |
| `DATABASE.md` | ETL processes, import commands, DB management |
| `GIS.md` | GIS data handling, coordinate storage, similarity distance |
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
