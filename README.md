# TaxProtest-Django

A Django web app for property tax analysis and comparison using Harris County Appraisal District (HCAD) data. It supports search, similarity matching, automated data imports, and rich building/feature details.

## Features

- Property search by owner, address, street, or ZIP
- Similar properties based on distance, size, age, and features
- Building details (sqft, year built, bedrooms, bathrooms, etc.)
- Extra features (pools, garages, patios, etc.)
- GIS coordinates (latitude/longitude) for location-aware results
- CSV export of search results
- Scheduled imports (Celery Beat)

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

The current production-ready ETL flow is the legacy management-command path, with a strict full import that now fails if residential building or GIS completeness is not achieved:

```bash
# Full residential-ready import
docker compose exec web python manage.py import_all_data

# Validate the current dataset
docker compose exec web python manage.py validate_data
```

Manual stages are still available when you need to rerun one part of the import:

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

If you are upgrading an older database that may still contain mixed or incomplete rows, preview and apply cleanup with:

```bash
# Preview legacy-row cleanup
docker compose exec web python manage.py reconcile_property_data

# Apply cleanup for legacy mixed/incomplete rows
docker compose exec web python manage.py reconcile_property_data --apply
```

The modular `etl_pipeline` command still exists for alternate and experimental workflows, but `import_all_data` + `validate_data` is the authoritative path today. See `DATABASE.md` for the full ETL guide.

## Usage

1. Browse to http://localhost:8000
2. Search by address/owner/ZIP
3. Open a result and click “Similar” to view comparable properties

Similarity considers:
- Distance (default within 5 miles)
- Size (±30% building area)
- Age (±10 years)
- Features (pools, garages, etc.)
- Bedrooms/Bathrooms

## Development

Project layout:

```
taxprotest/           # Django project (settings, URLs, Celery)
data/                 # Models, ETL, tasks, similarity
templates/            # HTML templates (Bootstrap 5)
downloads/            # HCAD data files (auto-created)
docker-compose.yml    # Docker services
```

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

## Documentation

- SETUP.md — installation and configuration
- DATABASE.md — imports, ETL processes, and DB management
- GIS.md — GIS data handling and location features

## Data sources

HCAD: https://download.hcad.org/data/

Data files used:
- Real_acct_owner.txt — Property records
- Real_building_land.zip — Building details and features
- Parcels.zip — GIS shapefiles with coordinates

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

- Never commit .env files or secrets
- Rotate Django secret keys if exposed
- Configure ALLOWED_HOSTS for production

## License

See LICENSE for details.

---

Made for property tax analysis in Harris County, Texas.
