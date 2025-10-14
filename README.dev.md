# Development notes (Windows / PowerShell)

Start Postgres + Redis locally with Docker Compose:

```powershell
docker-compose up -d
```

Install dependencies into your venv:

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt
```

Apply migrations and create a superuser:

```powershell
# inside activated venv
python manage.py migrate
python manage.py createsuperuser
```

Start a Celery worker (from project root):

```powershell
# with redis running from compose
celery -A taxprotest worker --loglevel=info
```

Trigger the HCAD download task from Django shell for testing:

```powershell
python manage.py shell
>>> from data.tasks import download_and_extract_hcad
>>> download_and_extract_hcad.delay()
```

Downloaded files and extracted folders will be in the project's `downloads/` directory.
