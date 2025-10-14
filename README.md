# TaxProtest-Django

Development notes (Windows / PowerShell)

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

The project uses a top-level `templates/` directory and a minimal `data` app with a sample Celery task at `data/tasks.py`.
# TaxProtest-Django