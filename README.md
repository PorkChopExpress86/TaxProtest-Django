# TaxProtest-Django

A Django web app for property tax analysis and comparison across Texas appraisal districts.
Harris County (HCAD) and Brazos County (BCAD) are supported today.

Each county brings its own ETL — different source files, different record layouts — but every
county gets the *same* three pages: property search, comparable properties, and an ARB protest
evidence report. That shared surface lives in `counties/common/`; a county joins by writing an
adapter, not a new set of views and templates.

## Features

- Property search by owner, address, street, or ZIP
- Similar properties ranked by a weighted similarity score (Excellent / Good / Fair / Partial / Poor)
- Building details (sqft, year built, bedrooms, bathrooms, quality, condition, etc.)
- Extra features (pools, garages, patios, etc.)
- GIS coordinates (latitude/longitude) for location-aware results
- Land-only property support with separate scoring weights
- ARB protest evidence report with equity analysis and tax-impact estimate
- CSV and PDF export of search results and protest comparables
- Admin ETL pipeline panel with GIS and building import triggers
- Scheduled imports (Celery Beat)
- Health check endpoints (`/healthz/`, `/readiness/`)
- About page (`/about/`)

## Quick start (Docker Compose)

1) Create an environment file and secret key

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

Add DJANGO_SECRET_KEY to your .env:

```bash
echo "DJANGO_SECRET_KEY=<your-generated-key>" >> .env
```

2) Start services (web, Postgres, Redis, Celery)

```bash
docker compose up --build
```

App will be available at http://localhost:8000

Services:
- web (Django)
- db (PostgreSQL)
- redis (Redis broker)
- worker (Celery worker)
- beat (Celery Beat scheduler)

## Data imports

The authoritative import path is `import_all_data`, which enforces residential building and GIS completeness before finishing:

```bash
# Full import
docker compose exec web python manage.py import_all_data

# Validate the current dataset
docker compose exec web python manage.py validate_data
```

Manual stages are available when you need to rerun one part of the import:

```bash
# Property records
docker compose exec web python manage.py load_hcad_real_acct

# GIS coordinates
docker compose exec web python manage.py load_gis_data

# Building details, features, and room counts
docker compose exec web python manage.py import_building_data

# Room counts only (fixtures.txt)
docker compose exec web python manage.py load_room_counts
```

For feature-specific repair and validation steps, see `docs/guides/FEATURE_IMPORT.md`.

If upgrading an older database that may contain mixed or incomplete rows, preview and apply cleanup with:

```bash
# Preview legacy-row cleanup
docker compose exec web python manage.py reconcile_property_data

# Apply cleanup
docker compose exec web python manage.py reconcile_property_data --apply
```

See `docs/guides/DATABASE.md` for the full ETL guide.

## Usage

1. Browse to http://localhost:8000
2. Search by address, owner, or ZIP
3. Open a result and click "Similar" to view comparable properties

Similarity scoring uses weighted factors for residential properties:

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

Distance is used as a **filter** (default 10 miles) but does not affect the score. Land-only properties use a separate weight set (land size 80%, features 10%, distance 10%).

Match labels: **Excellent** (≥84) · **Good** (≥70) · **Fair** (≥52) · **Partial** (≥36) · **Poor** (<36)

## Development

Project layout:

```
taxprotest/               # Django project (settings, URLs, Celery, site-wide views)
  var/                    #   project runtime state (Celery beat schedule)
counties/
  common/                 # Shared web layer: contracts, analysis, charts, views, exports
  harris/                 # Harris County (HCAD): models, ETL, similarity, adapter
    var/                  #   downloads, extracted source files, ETL logs, reports
  brazos/                 # Brazos County (BCAD): models, ETL, similarity, adapter
    var/                  #   downloads, extracted source files
templates/                # HTML templates (Tailwind); counties/ holds the shared pages
scripts/                  # Entrypoint, build-time download, setup, monitoring helpers
docker-compose.yml        # Docker services
```

Each county's downloaded and extracted data lives inside that county's app directory, under
`counties/<slug>/var/`. Nothing large lands in the project root. Every directory can be
redirected with an environment variable (`HCAD_DOWNLOAD_DIR`, `BCAD_EXTRACT_DIR`, …) — see
`taxprotest/runtime_paths.py`.

### Adding a county

1. Create `counties/<slug>/` as a Django app with its own models, ETL commands, and similarity scoring.
2. Write `counties/<slug>/adapter.py`: a `CountyProfile` (labels, search fields, table columns) plus
   a `CountyAdapter` translating your models into the neutral `Subject` / `Comp` records.
3. Add `counties/<slug>/urls.py` calling `county_urlpatterns(adapter)`, and include it from
   `taxprotest/urls.py`.

Search, comparables, protest report, CSV, and PDF all come for free. `counties/common/tests/`
asserts every registered county exposes the same route set.

Common commands:

```bash
docker compose exec web python manage.py shell
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose logs -f web
docker compose logs -f worker
docker compose logs -f beat
```

### Dev tooling (format, lint, types)

- Pre-commit hooks: Black, Ruff, EOF/trailing whitespace fixers
- Types: mypy with django-stubs

Install once locally:

```bash
pip install -r requirements.txt
pre-commit install
```

Run manually:

```bash
pre-commit run --all-files
mypy
```

### Tests

```bash
docker compose exec web python manage.py test
```

## Documentation

- `docs/guides/SETUP.md` — installation and configuration
- `docs/guides/DATABASE.md` — imports, ETL processes, and DB management
- `docs/guides/GIS.md` — GIS data handling and location features
- `docs/SIMILARITY_SCORING.md` — similarity algorithm details
- `docs/ETL_PIPELINE.md` — ETL pipeline architecture
- `docs/REVERSE_PROXY.md` — reverse proxy / production deployment notes
- `docs/guides/DEPLOYMENT.md` — production deployment walkthrough

## AI workflows

- `docs/ai-workflows.md` — practical guide for AI/Copilot workflows in this repo
- `.github/copilot-instructions.md` — repository-wide Copilot guardrails
- `.github/prompts/` — reusable prompt files for ingestion, comparables, Docker/dev workflow, and test creation

## Data sources

Harris County (HCAD): https://download.hcad.org/data/ — downloaded into `counties/harris/var/downloads/`

- `Real_acct_owner.txt` — Property records
- `Real_building_land.zip` — Building details and features
- `Parcels.zip` — GIS shapefiles with coordinates

Brazos County (BCAD): certified PACS export — downloaded into `counties/brazos/var/downloads/`

- `APPRAISAL_*.TXT` — fixed-width property, land, improvement, and entity files
- GIS parcel shapefile — coordinates and situs addresses

## Troubleshooting

```bash
# Rebuild containers
docker compose up --build

# View logs
docker compose logs

# Reset database (destructive)
docker compose down -v
docker compose up --build
```

## Security

- Never commit `.env` files or secrets
- Rotate Django secret keys if exposed
- Configure `ALLOWED_HOSTS` for production

## License

See LICENSE for details.

---

Made for property tax analysis in Texas.
