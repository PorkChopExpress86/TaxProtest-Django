# TaxProtest-Django

Development notes (Windows / PowerShell)

## Environment Configuration

Copy `.env.example` to `.env` and set a strong `DJANGO_SECRET_KEY` (you can generate one with Python:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

The application will refuse to start if `DJANGO_SECRET_KEY` is not defined, preventing accidental deployments with an unsafe default.

1) Start Postgres and Redis with Docker Compose:

```powershell
docker compose up -d
```

2) Install Python deps into your virtualenv:

```powershell
pip install -r requirements.txt
```

3) Run migrations and create a superuser:

```powershell
python manage.py migrate
python manage.py createsuperuser
```

4) Start a Celery worker (from project root):

```powershell
celery -A taxprotest worker -l info
```

5) Start Django dev server:

```powershell
python manage.py runserver
```

If using Docker Compose the `.env` file will be read automatically (the settings loader calls `load_dotenv`).

## Security Note

The framework secret key is no longer committed to the repository. Ensure you rotate any previously deployed key since it was present in history.

The project uses a top-level `templates/` directory and a minimal `data` app with a sample Celery task at `data/tasks.py`.
# TaxProtest-Django