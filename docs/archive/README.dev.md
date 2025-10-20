# Development notes (Windows / PowerShell)

## Standard Local Development: Docker Compose

**Recommended:** Use Docker Compose for all services (Django, Postgres, Redis).

```bash
docker compose up --build
```

This will start all services and run migrations automatically. The Django app will be available at `http://localhost:8000`.

## Manual Python Workflow (Advanced)
You may still use a virtualenv and run commands manually, but Docker Compose is the default and preferred method.

## Celery Worker
To run a Celery worker, uncomment the `worker` service in `docker-compose.yml` and run:
```bash
docker compose up worker
```

## HCAD Download Task (Manual)
If you want to trigger the HCAD download task manually:
```bash
docker compose exec web python manage.py shell
>>> from data.tasks import download_and_extract_hcad
>>> download_and_extract_hcad.delay()
```

Downloaded files and extracted folders will be in the project's `downloads/` directory.
