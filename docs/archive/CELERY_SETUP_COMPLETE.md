# Celery Scheduled Import Setup - Complete

## ✅ What Was Implemented

### 1. Celery Task for Monthly Imports
**File**: `data/tasks.py`

Created `download_and_import_building_data()` task that:
- Downloads Real_building_land.zip from HCAD (current year)
- Extracts building_res.txt and extra_features.txt
- Clears old building/feature data
- Imports millions of new records
- Reports progress via task state updates
- Returns detailed results including counts and errors

### 2. Celery Beat Schedule Configuration
**File**: `taxprotest/celery.py`

Configured beat scheduler with:
- Monthly schedule: 2nd Tuesday of each month at 2:00 AM Central Time
- Crontab expression: `day_of_week='tuesday', day_of_month='8-14', hour=2, minute=0`
- Timezone: America/Chicago (matches Houston)
- Task expires after 12 hours if not executed

### 3. Docker Services
**File**: `docker-compose.yml`

Added two new services:
- **worker**: Celery worker to process background tasks
  - Command: `celery -A taxprotest worker --loglevel=info`
  - Connects to Redis for task queue
  - Processes tasks from all apps
  
- **beat**: Celery beat scheduler for periodic tasks
  - Command: `celery -A taxprotest beat --loglevel=info`
  - Triggers scheduled tasks at configured times
  - Uses persistent schedule file (celerybeat-schedule)

### 4. Management Command
**File**: `data/management/commands/import_building_data.py`

Manual trigger command:
```bash
docker compose exec web python manage.py import_building_data
```

Runs the import task synchronously (not through Celery) for testing.

### 5. Documentation
Created comprehensive documentation:
- **SCHEDULED_IMPORTS.md**: Complete guide to the monthly import system
- **SIMILARITY_SEARCH.md**: Updated to note dependency on building data
- **.github/copilot-instructions.md**: Updated with Celery architecture

## 🚀 How to Use

### Start All Services

```bash
# Start everything (web, db, redis, worker, beat)
docker compose up -d --build

# Check status
docker compose ps
```

### Monitor Celery

```bash
# Watch worker logs
docker compose logs -f worker

# Watch beat scheduler logs
docker compose logs -f beat

# Watch all Celery logs
docker compose logs -f worker beat
```

### Manual Import (Don't Wait for Schedule)

```bash
# Option 1: Management command (synchronous)
docker compose exec web python manage.py import_building_data

# Option 2: Trigger via Celery (asynchronous)
docker compose exec web python scripts/test_celery_task.py

# Option 3: Direct Python
docker compose exec web python -c "
from data.tasks import download_and_import_building_data
result = download_and_import_building_data()
print(result)
"
```

### Check Scheduled Tasks

```bash
# See what tasks are registered
docker compose exec worker celery -A taxprotest inspect registered

# See beat schedule
docker compose logs beat | grep schedule
```

## 📋 Task Details

### What It Does

1. **Downloads** Real_building_land.zip (~500MB)
2. **Extracts** to downloads/Real_building_land/
3. **Clears** old BuildingDetail and ExtraFeature records
4. **Imports** building_res.txt (~2-3M records)
5. **Imports** extra_features.txt (~3-5M records)
6. **Reports** counts and any errors

### Expected Results

```python
{
    'download_url': 'https://download.hcad.org/data/CAMA/2025/Real_building_land.zip',
    'extracted_to': '/app/downloads/Real_building_land',
    'buildings_imported': 2500000,
    'features_imported': 4000000,
    'building_error': None,
    'features_error': None,
}
```

### Duration

- Download: 2-5 minutes
- Extract: 1-2 minutes
- Clear old data: 1-2 minutes
- Import buildings: 20-30 minutes
- Import features: 20-30 minutes
- **Total: 45-70 minutes**

## 🔧 Configuration

### Change Schedule

Edit `taxprotest/celery.py`:

```python
app.conf.beat_schedule = {
    'download-and-import-building-data-monthly': {
        'task': 'data.tasks.download_and_import_building_data',
        'schedule': crontab(
            day_of_week='tuesday',      # Day of week
            day_of_month='8-14',        # 2nd week (days 8-14)
            hour=2,                      # Hour (24-hour format)
            minute=0,                    # Minute
        ),
    },
}
```

**Common Schedules:**
- Every 1st of month: `day_of_month='1'`
- Every 15th: `day_of_month='15'`
- Every Monday: `day_of_week='monday'`
- 3:30 AM: `hour=3, minute=30`

### Change Timezone

Edit `taxprotest/celery.py`:

```python
app.conf.timezone = 'America/New_York'  # Eastern
app.conf.timezone = 'America/Los_Angeles'  # Pacific
app.conf.timezone = 'UTC'  # Universal Time
```

### Adjust Import Batch Size

Edit `data/tasks.py`:

```python
# Smaller batches use less memory but are slower
buildings_count = load_building_details(building_file, chunk_size=1000)
features_count = load_extra_features(features_file, chunk_size=1000)
```

## 📊 Monitoring

### View Logs

```bash
# Last 100 lines from worker
docker compose logs worker | tail -100

# Last 50 lines from beat
docker compose logs beat | tail -50

# Follow logs in real-time
docker compose logs -f worker beat
```

### Check Task Status

```bash
# Active tasks
docker compose exec worker celery -A taxprotest inspect active

# Registered tasks
docker compose exec worker celery -A taxprotest inspect registered

# Stats
docker compose exec worker celery -A taxprotest inspect stats
```

### Database Check

```bash
# Count imported records
docker compose exec web python -c "
from data.models import BuildingDetail, ExtraFeature
print(f'Buildings: {BuildingDetail.objects.count():,}')
print(f'Features: {ExtraFeature.objects.count():,}')
"
```

## 🧪 Testing

### Test Schedule (2nd Tuesday Detection)

The crontab `day_of_month='8-14'` ensures the task runs on the 2nd Tuesday:
- Week 1: Days 1-7 (contains 1st Tuesday)
- **Week 2: Days 8-14 (contains 2nd Tuesday) ← Our schedule**
- Week 3: Days 15-21 (contains 3rd Tuesday)
- Week 4: Days 22-28 (contains 4th Tuesday)

### Verify Schedule Logic

```python
from celery.schedules import crontab

schedule = crontab(day_of_week='tuesday', day_of_month='8-14', hour=2, minute=0)
print(f"Next run: {schedule.remaining_estimate(last_run_at)}")
```

### Test Import Manually

```bash
# Quick test (import just a few records for validation)
docker compose exec web python -c "
from data.etl import load_building_details
import tempfile
# Create small test file
with open('/tmp/test.txt', 'w') as f:
    f.write('acct|bld_num|heat_ar|bed_rm\n')
    f.write('123456789012345|1|2000|3\n')
count = load_building_details('/tmp/test.txt')
print(f'Imported {count} test records')
"
```

## 🔍 Troubleshooting

### Worker Not Starting

```bash
# Check logs
docker compose logs worker

# Restart worker
docker compose restart worker

# Rebuild if code changed
docker compose up -d --build worker
```

### Beat Not Scheduling

```bash
# Check beat logs
docker compose logs beat

# Verify schedule file
docker compose exec beat ls -la /app/celerybeat-schedule

# Delete schedule file and restart
docker compose exec beat rm -f /app/celerybeat-schedule
docker compose restart beat
```

### Task Fails

```bash
# Check worker logs for traceback
docker compose logs worker | grep -A 20 "ERROR"

# Check task state
docker compose exec web python -c "
from celery.result import AsyncResult
task = AsyncResult('task-id-here')
print(f'State: {task.state}')
print(f'Result: {task.result}')
print(f'Traceback: {task.traceback}')
"
```

### Import Takes Too Long

1. **Reduce batch size** (use less memory, more disk I/O)
2. **Increase worker memory** in docker-compose.yml:
   ```yaml
   worker:
     mem_limit: 4g
   ```
3. **Use faster storage** for downloads/ directory

### Database Lock Issues

If import fails with database locks:
```bash
# Stop all services except db
docker compose stop web worker beat

# Run import with single process
docker compose run --rm web python manage.py import_building_data

# Restart all services
docker compose up -d
```

## 🎯 Next Steps

### 1. Complete First Import

```bash
# Run the import now to populate the database
docker compose exec web python manage.py import_building_data
```

### 2. Test Similarity Search

Once building data is imported:
1. Search for a property on http://localhost:8000/
2. Click "Find Similar" button
3. Verify results show nearby properties with similarity scores

### 3. Monitor First Scheduled Run

The next 2nd Tuesday at 2 AM:
```bash
# Check beat logs to confirm task was scheduled
docker compose logs beat | grep "download-and-import"

# Check worker logs to see task execution
docker compose logs worker | grep "download_and_import_building_data"
```

### 4. Optional: Add Email Notifications

Edit `data/tasks.py` to send email when import completes:

```python
from django.core.mail import send_mail

@shared_task(bind=True)
def download_and_import_building_data(self):
    # ... existing code ...
    
    # Send notification
    send_mail(
        subject='Building Data Import Completed',
        message=f'Buildings: {results["buildings_imported"]}\nFeatures: {results["features_imported"]}',
        from_email='admin@example.com',
        recipient_list=['you@example.com'],
    )
```

### 5. Optional: Install Flower for Monitoring

Add web-based Celery monitoring:

```yaml
# In docker-compose.yml
flower:
  build: .
  command: celery -A taxprotest flower --port=5555
  ports:
    - "5555:5555"
  environment:
    - CELERY_BROKER_URL=redis://redis:6379/0
  depends_on:
    - redis
```

Then add to requirements.txt:
```
flower>=2.0
```

Access at http://localhost:5555

## 📚 Related Documentation

- [SCHEDULED_IMPORTS.md](SCHEDULED_IMPORTS.md) - Detailed import system documentation
- [SIMILARITY_SEARCH.md](SIMILARITY_SEARCH.md) - Property comparison feature
- [BUILDING_FEATURES_SETUP.md](BUILDING_FEATURES_SETUP.md) - Initial setup guide
- [.github/copilot-instructions.md](.github/copilot-instructions.md) - Developer workflows

## ✨ Summary

✅ **Celery worker** and **beat scheduler** are running  
✅ **Monthly scheduled task** configured for 2nd Tuesday at 2 AM  
✅ **Task downloads and imports** building details and extra features  
✅ **Management command** available for manual imports  
✅ **Comprehensive documentation** created  
✅ **Docker Compose** orchestrates all services  

The system is now ready to automatically keep your property database updated with the latest HCAD building data every month!
