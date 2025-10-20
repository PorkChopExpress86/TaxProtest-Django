# Monthly Building Data Import - Celery Scheduled Task

## Overview

The application now includes an **automated monthly import** of building details and extra features from Harris County Appraisal District (HCAD) data. This ensures the property database stays current with the latest building specifications, room counts, and amenities.

## Schedule

**When**: 2nd Tuesday of every month at 2:00 AM Central Time
**Why 2nd Tuesday**: Gives HCAD time to update their monthly data files at the beginning of the month

## What Gets Imported

### Building Details (building_res.txt)
- Living area (square feet)
- Year built, year remodeled
- Number of bedrooms and bathrooms
- Number of stories
- Building type, style, class
- Quality and condition codes
- Foundation type
- Exterior wall material
- Roof type and cover
- Number of fireplaces

### Extra Features (extra_features.txt)
- Swimming pools
- Garages and carports
- Patios and decks
- Sheds and storage buildings
- Other amenities and improvements

## Architecture

### Components

1. **Celery Task** (`data/tasks.py`)
   - `download_and_import_building_data()`: Main scheduled task
   - Downloads Real_building_land.zip from HCAD
   - Extracts ZIP to downloads/Real_building_land/
   - Clears old building/feature data
   - Imports new data from building_res.txt and extra_features.txt

2. **Celery Beat Scheduler** (`taxprotest/celery.py`)
   - Configured schedule using crontab
   - Runs on 2nd Tuesday (days 8-14 of month)
   - Timezone: America/Chicago (Central Time)

3. **ETL Functions** (`data/etl.py`)
   - `load_building_details()`: Parses building_res.txt
   - `load_extra_features()`: Parses extra_features.txt
   - Bulk insert with 5000 record batches for efficiency

4. **Docker Services** (`docker-compose.yml`)
   - `worker`: Celery worker to process tasks
   - `beat`: Celery beat scheduler to trigger monthly tasks

## Running the Services

### Start All Services (including Celery)

```bash
docker compose up --build
```

This starts:
- `web`: Django application
- `db`: PostgreSQL database
- `redis`: Redis message broker
- `worker`: Celery worker for task processing
- `beat`: Celery beat scheduler for periodic tasks

### Run Import Manually

If you want to trigger the import immediately (not wait for scheduled time):

```bash
# Using the management command
docker compose exec web python manage.py import_building_data

# Or trigger via Celery task
docker compose exec web python -c "
from data.tasks import download_and_import_building_data
download_and_import_building_data()
"
```

### Check Celery Worker Status

```bash
# View worker logs
docker compose logs -f worker

# View beat scheduler logs
docker compose logs -f beat
```

## Task Flow

1. **Download Phase**
   - Fetches Real_building_land.zip from HCAD (current year)
   - URL: `https://download.hcad.org/data/CAMA/{year}/Real_building_land.zip`
   - Saves to `downloads/Real_building_land.zip`

2. **Extract Phase**
   - Unzips to `downloads/Real_building_land/`
   - Creates DownloadRecord in database

3. **Clear Phase**
   - Deletes all existing BuildingDetail records
   - Deletes all existing ExtraFeature records
   - This ensures no stale data remains

4. **Import Building Details**
   - Reads `building_res.txt`
   - Parses ~millions of building records
   - Links to PropertyRecord via account_number
   - Bulk inserts in 5000-record batches
   - Prints progress every 5000 records

5. **Import Extra Features**
   - Reads `extra_features.txt`
   - Parses ~millions of feature records
   - Links to PropertyRecord via account_number
   - Bulk inserts in 5000-record batches

6. **Completion**
   - Returns count of imported records
   - Task state updated to SUCCESS

## Task State Tracking

The Celery task updates its state during execution:

- `DOWNLOADING`: Fetching ZIP file from HCAD
- `EXTRACTING`: Unzipping the archive
- `CLEARING`: Removing old data
- `IMPORTING`: Loading new data (building details, then features)
- `SUCCESS`: Import completed

You can monitor task progress using Celery Flower (optional monitoring tool) or by checking logs.

## Data Volume

Expected import sizes:
- **Building Details**: ~2-3 million records
- **Extra Features**: ~3-5 million records
- **Total Import Time**: 30-60 minutes (depending on server specs)
- **Disk Space**: ~500MB for ZIP, ~2GB extracted

## Error Handling

The task includes error handling for:

- **Download failures**: Retries with exponential backoff
- **Extraction errors**: Logs error and exits
- **Missing files**: Reports which files are missing
- **Import errors**: Continues with partial data, reports errors in results

Results dictionary includes error fields:
```python
{
    'download_url': 'https://...',
    'extracted_to': '/app/downloads/Real_building_land',
    'buildings_imported': 2500000,
    'features_imported': 4000000,
    'building_error': None,  # or error message
    'features_error': None,  # or error message
}
```

## Database Impact

### Before Import
- BuildingDetail table: May have millions of records
- ExtraFeature table: May have millions of records

### During Import
- All old records are **deleted** before importing new ones
- This ensures data consistency (no mix of old/new data)
- Database is briefly empty between delete and new inserts

### After Import
- BuildingDetail: Populated with latest HCAD data
- ExtraFeature: Populated with latest HCAD data
- Similarity search uses this fresh data

## Configuration

### Schedule Customization

To change the schedule, edit `taxprotest/celery.py`:

```python
app.conf.beat_schedule = {
    'download-and-import-building-data-monthly': {
        'task': 'data.tasks.download_and_import_building_data',
        'schedule': crontab(
            day_of_week='tuesday',      # Change day of week
            day_of_month='8-14',        # Change week (8-14 = 2nd week)
            hour=2,                      # Change hour (24-hour format)
            minute=0,                    # Change minute
        ),
    },
}
```

**Crontab Examples:**
- `day_of_month='1'` - 1st of every month
- `day_of_month='15'` - 15th of every month
- `day_of_week='monday'` - Every Monday
- `hour=3, minute=30` - At 3:30 AM

### Timezone Configuration

The scheduler uses `America/Chicago` (Central Time) to match Houston's timezone. To change:

```python
app.conf.timezone = 'America/New_York'  # Eastern Time
```

## Monitoring & Logging

### View Real-time Logs

```bash
# All Celery services
docker compose logs -f worker beat

# Just worker
docker compose logs -f worker

# Just scheduler
docker compose logs -f beat
```

### Check Scheduled Tasks

```bash
docker compose exec beat celery -A taxprotest inspect scheduled
```

### Check Active Tasks

```bash
docker compose exec worker celery -A taxprotest inspect active
```

## Testing the Schedule

### Test Manually (Don't Wait for 2nd Tuesday)

```bash
# Run the import now
docker compose exec web python manage.py import_building_data
```

### Force Celery to Run Task Now

```bash
docker compose exec web python -c "
from data.tasks import download_and_import_building_data
task = download_and_import_building_data.delay()
print(f'Task ID: {task.id}')
"
```

### Check Task Result

```bash
docker compose exec web python -c "
from celery.result import AsyncResult
task = AsyncResult('task-id-here')
print(f'State: {task.state}')
print(f'Result: {task.result}')
"
```

## Production Considerations

### 1. Notification on Completion

Add email notifications when import completes:

```python
# In data/tasks.py
from django.core.mail import send_mail

@shared_task(bind=True)
def download_and_import_building_data(self):
    # ... existing code ...
    
    # Send email notification
    send_mail(
        'Monthly Building Data Import Completed',
        f'Buildings: {results["buildings_imported"]}\nFeatures: {results["features_imported"]}',
        'admin@example.com',
        ['you@example.com'],
    )
```

### 2. Backup Before Import

Create backup before deleting old data:

```bash
docker compose exec db pg_dump -U taxprotest taxprotest > backup_$(date +%Y%m%d).sql
```

Or automate in the task:

```python
import subprocess
subprocess.run(['pg_dump', '-U', 'taxprotest', 'taxprotest', '-f', f'backup_{datetime.now():%Y%m%d}.sql'])
```

### 3. Monitoring Tools

Install Celery Flower for web-based monitoring:

```bash
pip install flower

# Add to docker-compose.yml
flower:
  build: .
  command: celery -A taxprotest flower --port=5555
  ports:
    - "5555:5555"
  depends_on:
    - redis
```

Access at http://localhost:5555

### 4. Resource Limits

For large imports, consider setting Celery worker limits:

```python
# In celery.py
app.conf.worker_max_tasks_per_child = 1  # Restart worker after each task
app.conf.task_time_limit = 7200  # 2 hour timeout
app.conf.task_soft_time_limit = 6600  # 1h 50m soft timeout
```

## Troubleshooting

### Task Not Running on Schedule

1. Check beat scheduler is running:
   ```bash
   docker compose ps beat
   ```

2. Check beat logs:
   ```bash
   docker compose logs beat | grep "download-and-import"
   ```

3. Verify schedule:
   ```bash
   docker compose exec beat celery -A taxprotest inspect scheduled
   ```

### Import Fails

1. Check worker logs:
   ```bash
   docker compose logs worker | tail -100
   ```

2. Verify file exists:
   ```bash
   docker compose exec web ls -lah downloads/Real_building_land/
   ```

3. Test ETL functions directly:
   ```bash
   docker compose exec web python -c "
   from data.etl import load_building_details
   load_building_details('downloads/Real_building_land/building_res.txt')
   "
   ```

### High Memory Usage

If imports consume too much memory:

1. Reduce chunk size in ETL:
   ```python
   load_building_details(filepath, chunk_size=1000)  # Smaller chunks
   ```

2. Increase Docker memory limit:
   ```yaml
   # docker-compose.yml
   worker:
     mem_limit: 4g
   ```

## Future Enhancements

1. **Incremental Updates**: Instead of deleting all records, detect changes and update only modified records
2. **Change Detection**: Track what changed between imports and notify users
3. **Historical Data**: Keep old versions in separate tables for historical analysis
4. **Parallel Processing**: Split imports across multiple workers for faster processing
5. **Data Validation**: Verify imported data quality and flag anomalies
6. **Rollback Support**: Keep previous version and allow rollback if new import has issues

## Related Documentation

- [SIMILARITY_SEARCH.md](SIMILARITY_SEARCH.md) - Uses building/feature data for property comparison
- [BUILDING_FEATURES_SETUP.md](BUILDING_FEATURES_SETUP.md) - Initial setup guide
- [GIS_SETUP.md](GIS_SETUP.md) - GIS data import setup

## Summary

The monthly scheduled import keeps your property database current with the latest HCAD building data. It runs automatically on the 2nd Tuesday of each month at 2 AM, downloading and importing millions of building details and extra features records. The Celery-based architecture ensures reliable background processing with proper error handling and monitoring.
