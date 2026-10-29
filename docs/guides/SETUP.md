# Setup Guide - TaxProtest-Django

Complete installation and configuration guide for setting up the TaxProtest-Django application.

## Table of Contents

- [Quick Start](#quick-start)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Docker Services](#docker-services)
- [Initial Data Import](#initial-data-import)
- [Production Deployment](#production-deployment)
- [Troubleshooting](#troubleshooting)

## Quick Start
You can set up the entire project, including data download and import, by running the automated setup script:
```bash
./scripts/setup.sh
```
This script handles container building and migrations. The data import will start automatically when the containers launch for the first time.


## Prerequisites

### Required Software

**Docker & Docker Compose:**
```bash
# Install Docker (Ubuntu/Debian)
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER

# Install Docker Compose
sudo apt-get install docker-compose-plugin

# Verify installation
docker --version
docker compose version
```

**For MacOS:**
- Install Docker Desktop from https://www.docker.com/products/docker-desktop

**For Windows:**
- Install Docker Desktop from https://www.docker.com/products/docker-desktop
- Enable WSL2 backend

### System Requirements

- **RAM:** 4GB minimum, 8GB recommended
- **Disk Space:** 10GB minimum, 20GB recommended
- **CPU:** 2+ cores recommended
- **OS:** Linux, macOS, or Windows 10/11 with WSL2

## Installation

### 1. Clone Repository

```bash
git clone https://github.com/PorkChopExpress86/TaxProtest-Django.git
cd TaxProtest-Django
```

### 2. Create Environment File

```bash
python3 scripts/env_setup.py
```

`scripts/env_setup.py` copies `.env.example` to `.env` when there is none, and generates
`DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD` wherever they are missing, blank or still
`change-me`. Other values and comments are kept, the file is written with mode 0600, and no
secret is printed. It is safe to run at any time.

To replace the generated secrets, for example after one leaks:

```bash
python3 scripts/env_setup.py --rotate                     # both
python3 scripts/env_setup.py --rotate POSTGRES_PASSWORD   # just one
python3 scripts/env_setup.py -f docker-compose.prod.yml --rotate   # production stack
```

Postgres reads `POSTGRES_PASSWORD` only when it creates the `pgdata` volume, so rotation
first changes the database role's password (the `postgres` service must be running) and
leaves `.env` unchanged if that fails. It then recreates the running app services so they
use the new values. A new `DJANGO_SECRET_KEY` signs everyone out. Pass `--no-restart` to
print the recreate command instead. The Django admin password is stored in the database:
change it with `docker compose exec web python manage.py changepassword <username>`.

**Required `.env` variables** (Compose refuses to start if either is unset):
```bash
DJANGO_SECRET_KEY=<your-generated-key-here>
POSTGRES_PASSWORD=<set-a-strong-postgres-password>
```

`POSTGRES_USER` and `POSTGRES_DB` default to `taxprotest`. `docker-compose.yml` builds `DATABASE_URL` (host `postgres`) and the Celery broker/result URLs (`redis://redis:6379/0`) from these values, so you do not set them in `.env`. See `.env.example` for the remaining optional settings, including `DEBUG` and `ALLOWED_HOSTS`.

### 3. Build and Start Services

```bash
# Build Docker containers
docker compose build

# Start all services
docker compose up -d

# View logs
docker compose logs -f
```

Services will start on:
- **Django:** http://localhost:8020
- **PostgreSQL:** 127.0.0.1:6385
- **Redis:** 127.0.0.1:6379

### 4. Run Database Migrations

```bash
# Apply Django migrations
docker compose exec web python manage.py migrate

# Create superuser for admin access
docker compose exec web python manage.py createsuperuser
```

### 5. Verify Installation

Visit http://localhost:8020 - you should see the property search page.

> [!TIP]
> **Alternative: Automated Setup**
> Instead of running steps 3, 4, and the Data Import section manually, you can simply run `./scripts/setup.sh` from the project root.


## Configuration

### Django Settings

Edit `taxprotest/settings.py` for advanced configuration:

**Database:** `settings.py` defaults to SQLite and switches to PostgreSQL when `DATABASE_URL` is set. Compose sets it for every service, so the database is configured through `.env` (`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`) rather than by editing `settings.py`.

**Celery:**
```python
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
```

The schedule timezone (`America/Chicago`) is set in `taxprotest/celery.py`.

**Static Files:**
```python
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
```

### Celery Beat Schedule

Edit `taxprotest/celery.py` to customize automated task schedules:

```python
app.conf.beat_schedule = {
    # Monthly building data import
    'download-and-import-building-data-monthly': {
        'task': 'counties.harris.tasks_new.run_etl_pipeline',
        'kwargs': {'scope': 'building-only', 'strict': True},
        'schedule': crontab(
            day_of_week='tuesday',
            day_of_month='8-14',  # 2nd Tuesday
            hour=2,
            minute=0,
        ),
        'options': {'expires': 3600 * 12},
    },
    # Annual GIS update
    'download-and-import-gis-data-annually': {
        'task': 'counties.harris.tasks_new.run_etl_pipeline',
        'kwargs': {'scope': 'gis-only', 'strict': True},
        'schedule': crontab(
            month_of_year='1',  # January
            day_of_month='15',
            hour=3,
            minute=0,
        ),
        'options': {'expires': 3600 * 24},
    },
}
```

## Docker Services

### Service Overview

**web** - Django application
- Host port: 8020 (container port 8000)
- Depends on: postgres, redis
- Runs: migrations, a data check, then gunicorn (via `scripts/entrypoint.sh`)

**postgres** - PostgreSQL database
- Host port: 127.0.0.1:6385 (container port 5432)
- Data: Persisted in Docker volume `pgdata`
- User and password: `POSTGRES_USER` / `POSTGRES_PASSWORD` from `.env`

**redis** - Message broker
- Host port: 127.0.0.1:6379
- Used by: Celery for task queuing

**worker** - Celery worker
- Processes: Background tasks
- Depends on: postgres, redis

**beat** - Celery Beat scheduler
- Schedules: Automated periodic tasks
- Depends on: postgres, redis

### Managing Services

```bash
# Start all services
docker compose up -d

# Start specific services
docker compose up -d web postgres redis

# Stop all services
docker compose down

# Stop and remove volumes (deletes database!)
docker compose down -v

# View logs
docker compose logs -f web
docker compose logs -f worker
docker compose logs -f beat

# Restart a service
docker compose restart web

# Rebuild containers
docker compose up --build

# Check service status
docker compose ps
```

### Resource Limits

Edit `docker-compose.yml` to adjust resource limits:

```yaml
services:
  web:
    mem_limit: 2g
    cpus: '1.0'
  
  worker:
    mem_limit: 4g
    cpus: '2.0'
```

## Initial Data Import

### Step 1: Run the Full Residential-Ready Import

```bash
docker compose exec web python manage.py import_all_data
```

This runs the current authoritative ETL flow and now fails hard if building or GIS completeness is not achieved.

### Step 2: Validate the Imported Dataset

```bash
docker compose exec web python manage.py validate_data
```

Validation enforces the residential-only, data-ready contract:
- residential properties only
- active residential building records present
- bedroom and bathroom counts populated
- GIS coordinates present

### Step 3: Clean Up Older Mixed/Incomplete Rows (If Needed)

```bash
# Preview cleanup
docker compose exec web python manage.py reconcile_property_data

# Apply cleanup to legacy mixed/incomplete rows
docker compose exec web python manage.py reconcile_property_data --apply
```

Use this when upgrading an older database that may still contain non-residential rows or residential records missing building, room-count, or GIS data.

### Import Progress Monitoring

```bash
# Watch import progress
docker compose logs -f web

# Check worker logs for Celery tasks
docker compose logs -f worker

# Check database counts
docker compose exec web python manage.py shell -c "
from counties.harris.models import PropertyRecord, BuildingDetail, ExtraFeature
print(f'Properties: {PropertyRecord.objects.count():,}')
print(f'Buildings: {BuildingDetail.objects.count():,}')
print(f'Features: {ExtraFeature.objects.count():,}')
"
```

## Production Deployment

### Security Checklist

- [ ] Set `DEBUG=False` in `.env`
- [ ] Generate strong `DJANGO_SECRET_KEY`
- [ ] Configure `ALLOWED_HOSTS` properly
- [ ] Use HTTPS (configure nginx/reverse proxy)
- [ ] Set up proper database backups
- [ ] Use strong PostgreSQL password
- [ ] Enable firewall rules
- [ ] Set up monitoring and logging
- [ ] Configure CORS if needed
- [ ] Review Django security settings

### Production Settings

```python
# In production settings:
DEBUG = False
ALLOWED_HOSTS = ['yourdomain.com', 'www.yourdomain.com']
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
```

### Nginx Configuration Example

```nginx
server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com;
    
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    location / {
        proxy_pass http://localhost:8020;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    
    location /static/ {
        # staticfiles/ is generated at container build time via collectstatic
        # Mount or copy from the web container, e.g. /path/to/staticfiles/
        alias /path/to/staticfiles/;
    }
}
```

### Database Backups

```bash
# Backup database
docker compose exec postgres pg_dump -U taxprotest taxprotest > backup_$(date +%Y%m%d).sql

# Restore database
docker compose exec -T postgres psql -U taxprotest taxprotest < backup_20251016.sql

# Automated daily backups (crontab)
0 3 * * * cd /path/to/project && docker compose exec postgres pg_dump -U taxprotest taxprotest > backups/backup_$(date +\%Y\%m\%d).sql
```

## Troubleshooting

### Docker Issues

**Containers won't start:**
```bash
# Check Docker daemon
sudo systemctl status docker

# Check logs
docker compose logs

# Rebuild from scratch
docker compose down -v
docker compose build --no-cache
docker compose up
```

**Permission denied:**
```bash
# Add user to docker group
sudo usermod -aG docker $USER
newgrp docker
```

### Database Issues

**Connection refused:**
```bash
# Check PostgreSQL is running
docker compose ps postgres

# Check logs
docker compose logs postgres

# Restart database
docker compose restart postgres
```

**Migrations fail:**
```bash
# Reset migrations (WARNING: deletes data!)
docker compose down -v
docker compose up -d postgres
docker compose exec web python manage.py migrate
```

### Import Issues

**Import stuck/slow:**
- Check disk space: `df -h`
- Check memory: `docker stats`
- Increase Docker memory limit
- Check network connection (for downloads)

**Import fails with errors:**
```bash
# Check worker logs
docker compose logs worker

# Check disk space
df -h

# Verify downloaded files
ls -lh counties/harris/var/downloads/

# Re-run import
docker compose exec web python manage.py import_building_data
```

### Performance Issues

**Web server slow:**
- Check database queries in Django Debug Toolbar
- Add database indexes if needed
- Increase pagination size
- Check for missing data imports

**Worker memory issues:**
```yaml
# Increase worker memory in docker-compose.yml
worker:
  mem_limit: 4g
```

### Common Errors

**"No module named ..."**
```bash
# Rebuild containers
docker compose up --build
```

**"OperationalError: FATAL: database does not exist"**
```bash
# Create database
docker compose exec postgres createdb -U taxprotest taxprotest
docker compose exec web python manage.py migrate
```

**"Port already in use"**
```bash
# Change port in docker-compose.yml
ports:
  - "8001:8000"  # Use port 8001 instead of 8020
```

## Getting Help

- Check [DATABASE.md](DATABASE.md) for data import issues
- Check [GIS.md](GIS.md) for location/mapping issues
- View logs: `docker compose logs -f`
- Check Django admin: http://localhost:8020/admin/
- Review HCAD documentation in `counties/harris/var/extracted/Code_description_*/`

---

**Setup complete! 🎉** Visit http://localhost:8020 to start using the application.
