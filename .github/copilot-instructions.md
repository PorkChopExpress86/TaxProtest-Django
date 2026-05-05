
# Copilot Instructions for TaxProtest-Django

## Project Overview
- Django web application for property tax protest workflows, currently being adapted for a "homevalues" web app.
- Main Django project is in `taxprotest/`. All templates are in the top-level `templates/` directory.
- The codebase is intentionally simple and extensible, with scheduled imports already implemented via Celery.

## Container-only development rule

This project runs in Docker. Do not assume Python, pip, pytest, ruff, mypy, Postgres, GDAL, or other dependencies are installed on the host.

Use Docker Compose for all development, testing, ingestion, refresh, and app commands.

Preferred commands:

```bash
docker compose build
docker compose up -d postgres
docker compose up -d taxprotest-dev
docker compose run --rm ingest
docker compose run --rm refresh
docker compose run --rm taxprotest-dev pytest -q
docker compose run --rm taxprotest-dev ruff check .
docker compose run --rm taxprotest-dev black --check .
docker compose run --rm taxprotest-dev mypy src

## Architecture & Key Components
- `taxprotest/`: Contains Django settings, URL routing, and the main view (`views.py`).
- `templates/`: All user-facing HTML templates. `base.html` provides the Bootstrap-based layout; `index.html` is the main entry page and extends `base.html`.
- `data/`: Django app for property data models, ETL functions, and Celery tasks.
  - `models.py`: PropertyRecord, BuildingDetail, ExtraFeature
  - `etl.py`: Data import functions for HCAD files
  - `tasks_new.py`: Celery tasks for scheduled imports and ETL helpers
  - `similarity.py`: Property comparison algorithm
- **Celery**: Background task processing with Redis as message broker
  - Configured in `taxprotest/celery.py` with Beat scheduler for periodic tasks
  - Monthly scheduled import of building data (2nd Tuesday at 2 AM)


## Developer Workflows
**Standard: Always use Docker Compose for running the app and services.**
- **Start all services:** `docker compose up --build` (runs Django, Postgres, Redis, Celery worker, Celery beat, migrations)
- **Start without Celery:** `docker compose up web db redis` (if you don't need background tasks)
- **Run full import manually:** `docker compose exec web python manage.py import_all_data`
- **Validate imported data:** `docker compose exec web python manage.py validate_data`
- **Reconcile older mixed/incomplete rows:** `docker compose exec web python manage.py reconcile_property_data --apply`
- **Check Celery logs:** `docker compose logs -f worker` or `docker compose logs -f beat`
- **Manual commands:** Only use manual Python commands if explicitly requested; Docker Compose is default.
- **Add new apps:** `python manage.py startapp <appname>` (can be run inside the web container)

## Templates & UI Patterns
- Templates are loaded from the top-level `templates/` directory (see `settings.py:TEMPLATES['DIRS']`).
- Always use `{% extends "base.html" %}` for new templates to ensure consistent Bootstrap layout and navigation.
- UI uses Bootstrap 5 via CDN (see `base.html`).

## Views & Routing
- Main view is `taxprotest/views.py:index`, mapped to the root URL in `taxprotest/urls.py`.
- Add new views to `taxprotest/views.py` or a new app's `views.py` as needed, and update `urls.py` accordingly.

## Project Conventions
- All user-facing templates go in the global `templates/` directory.
- Use Bootstrap for all UI components.
- Reference views in `urls.py` using `from <app>.views import ...` and map URLs explicitly.
- Use environment variables or Django settings for secrets/configuration (see `settings.py`).

## Background Tasks
- **Celery** is configured for background task processing using Redis as the message broker.
- **Celery Beat** scheduler runs periodic tasks (see `taxprotest/celery.py` for schedule).
- **Monthly Import Task**: Automatically downloads and imports building details and extra features from HCAD on the 2nd Tuesday of each month at 2 AM Central Time.
- **Annual GIS Import**: Downloads and updates property coordinates on January 15th at 3 AM Central Time.
- Task location: `data/tasks_new.py:download_and_import_building_data()` and `data/tasks_new.py:download_and_import_gis_data()`
- To add new scheduled tasks, update `beat_schedule` in `taxprotest/celery.py`
- See `docs/guides/DATABASE.md` for detailed documentation on data imports and scheduled tasks.

## Documentation Structure
- **README.md** - Project overview, features, quick start guide, and usage instructions
- **docs/guides/SETUP.md** - Installation, configuration, Docker services, and production deployment
- **docs/guides/DATABASE.md** - Data sources, import processes, ETL functions, and database management
- **docs/guides/GIS.md** - GIS features, location data, coordinate handling, and similarity search
- **docs/archive/** - Historical documentation (archived)

## Example File Structure
```
TaxProtest-Django/
├── manage.py
├── README.md              # Start here
├── docs/guides/SETUP.md   # Installation guide
├── docs/guides/DATABASE.md # Data import documentation
├── docs/guides/GIS.md     # GIS features documentation
├── taxprotest/
│   ├── settings.py
│   ├── urls.py
│   ├── views.py
│   ├── celery.py          # Background tasks config
│   └── ...
├── data/
│   ├── models.py          # PropertyRecord, BuildingDetail, ExtraFeature
│   ├── etl.py             # Data import functions
│   ├── tasks_new.py       # Celery tasks
│   ├── similarity.py      # Property comparison algorithm
│   └── management/
│       └── commands/      # Import commands
├── templates/
│   ├── base.html
│   ├── index.html
│   └── similar_properties.html
└── docs/
    └── archive/           # Historical documentation
```

---
**Agents and developers should always default to Docker Compose for running, testing, and deploying the app.**
For detailed information, consult README.md, docs/guides/SETUP.md, docs/guides/DATABASE.md, and docs/guides/GIS.md.
If you add new conventions, workflows, or apps, update this file to keep AI agents productive.

## Update prompt files

Every prompt should use container commands.

For example, `optimize-ingestion.prompt.md` should say:

```md
Run validation inside Docker:

```bash
docker compose build
docker compose up -d postgres
docker compose run --rm ingest
docker compose run --rm taxprotest-dev pytest -q
docker compose run --rm taxprotest-dev ruff check .
docker compose run --rm taxprotest-dev mypy src