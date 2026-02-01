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
./setup.sh
```
This script handles container building, migrations, and data imports. It provides progress feedback during large file downloads.


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
# Copy example environment file
cp .env.example .env

# Generate a secure secret key
python3 -c "import secrets; print(secrets.token_urlsafe(64))"

# Edit .env and add the generated key
nano .env  # or use your favorite editor
```

**Required `.env` variables:**
```bash
DJANGO_SECRET_KEY=<your-generated-key-here>
DEBUG=True  # Set to False in production
ALLOWED_HOSTS=localhost,127.0.0.1
POSTGRES_DB=taxprotest
POSTGRES_USER=taxprotest
POSTGRES_PASSWORD=<set-a-strong-postgres-password>
DATABASE_URL=postgresql://taxprotest:<set-a-strong-postgres-password>@db:5432/taxprotest
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0
```

Use the same credentials in `DATABASE_URL` that you set for `POSTGRES_USER` and `POSTGRES_PASSWORD` so Django and the database stay aligned.

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
- **Django:** http://localhost:8000
- **PostgreSQL:** localhost:5432
- **Redis:** localhost:6379

### 4. Run Database Migrations

```bash
# Apply Django migrations
docker compose exec web python manage.py migrate

# Create superuser for admin access
docker compose exec web python manage.py createsuperuser
```

### 5. Verify Installation

### 5. Verify Installation

Visit http://localhost:8000 - you should see the property search page.

> [!TIP]
> **Alternative: Automated Setup**
> Instead of running steps 3, 4, and the Data Import section manually, you can simply run `./setup.sh` from the project root.


## Configuration

### Django Settings

Edit `taxprotest/settings.py` for advanced configuration:

**Database:**
```python
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'taxprotest',
        'USER': 'taxprotest',
        'PASSWORD': 'password',
        'HOST': 'db',
        'PORT': '5432',
    }
}
```

**Celery:**
```python
CELERY_BROKER_URL = 'redis://redis:6379/0'
CELERY_RESULT_BACKEND = 'redis://redis:6379/0'
CELERY_TIMEZONE = 'America/Chicago'
```

**Static Files:**
```python
STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
```

### Celery Beat Schedule

Edit `taxprotest/celery.py` to customize automated task schedules:

```python
app.conf.beat_schedule = {
    # Monthly building data import
    'download-and-import-building-data-monthly': {
        'task': 'data.tasks.download_and_import_building_data',
        'schedule': crontab(
            day_of_week='tuesday',
            day_of_month='8-14',  # 2nd Tuesday
            hour=2,
            minute=0,
        ),
    },
    # Annual GIS update
    'download-and-import-gis-data-annually': {
        'task': 'data.tasks.download_and_import_gis_data',
        'schedule': crontab(
            month_of_year='1',  # January
            day_of_month='15',
            hour=3,
            minute=0,
        ),
    },
}
```

## Docker Services

### Service Overview

**web** - Django application
- Port: 8000
- Depends on: db, redis
- Runs: Django development server

**db** - PostgreSQL database
- Port: 5432
- Data: Persisted in Docker volume `postgres_data`
- User: taxprotest / password

**redis** - Message broker
- Port: 6379
- Used by: Celery for task queuing

**worker** - Celery worker
- Processes: Background tasks
- Depends on: db, redis

**beat** - Celery Beat scheduler
- Schedules: Automated periodic tasks
- Depends on: redis

### Managing Services

```bash
# Start all services
docker compose up -d

# Start specific services
docker compose up -d web db redis

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

### Step 1: Import Property Records (Required)

```bash
# Import ~1.6M property records from HCAD
docker compose exec web python manage.py import_hcad_data
```

This takes 15-30 minutes and imports:
- Owner names and addresses
- Property locations (street, zip)
- Assessed values
- Building and land area

### Step 2: Import GIS Data (Recommended)

```bash
# Import latitude/longitude coordinates
docker compose exec web python manage.py load_gis_data
```

This takes 30-45 minutes and adds:
- Latitude/longitude for each property
- Enables location-based similarity search
- Required for distance calculations

### Step 3: Import Building Details (Recommended)

```bash
# Import building specs and features
docker compose exec web python manage.py import_building_data
```

This takes 60-90 minutes and adds:
- Building area, year built
- Bedrooms and bathrooms
- Extra features (pools, garages, etc.)
- Enables feature-based similarity matching

### Import Progress Monitoring

```bash
# Watch import progress
docker compose logs -f web

# Check worker logs for Celery tasks
docker compose logs -f worker

# Check database counts
docker compose exec web python manage.py shell -c "
from data.models import PropertyRecord, BuildingDetail, ExtraFeature
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
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    
    location /static/ {
        alias /path/to/staticfiles/;
    }
}
```

### Database Backups

```bash
# Backup database
docker compose exec db pg_dump -U taxprotest taxprotest > backup_$(date +%Y%m%d).sql

# Restore database
docker compose exec -T db psql -U taxprotest taxprotest < backup_20251016.sql

# Automated daily backups (crontab)
0 3 * * * cd /path/to/project && docker compose exec db pg_dump -U taxprotest taxprotest > backups/backup_$(date +\%Y\%m\%d).sql
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
docker compose ps db

# Check logs
docker compose logs db

# Restart database
docker compose restart db
```

**Migrations fail:**
```bash
# Reset migrations (WARNING: deletes data!)
docker compose down -v
docker compose up -d db
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
ls -lh downloads/

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
docker compose exec db createdb -U taxprotest taxprotest
docker compose exec web python manage.py migrate
```

**"Port already in use"**
```bash
# Change port in docker-compose.yml
ports:
  - "8001:8000"  # Use port 8001 instead
```

## Getting Help

- Check [DATABASE.md](DATABASE.md) for data import issues
- Check [GIS.md](GIS.md) for location/mapping issues
- View logs: `docker compose logs -f`
- Check Django admin: http://localhost:8000/admin/
- Review HCAD documentation in `downloads/Code_description_*/`

---

**Setup complete! 🎉** Visit http://localhost:8000 to start using the application.
