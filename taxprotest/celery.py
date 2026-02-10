import os
from celery import Celery
from celery.schedules import crontab

# set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taxprotest.settings')

app = Celery('taxprotest')

# Keep celery related config under environment variables prefixed with CELERY_
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django app configs.
app.autodiscover_tasks()


# Periodic task schedule
app.conf.beat_schedule = {
    'download-and-import-building-data-monthly': {
        'task': 'data.tasks_new.download_and_import_building_data',
        'schedule': crontab(
            day_of_week='tuesday',      # Tuesday
            day_of_month='8-14',        # 2nd Tuesday (days 8-14)
            hour=2,                      # 2 AM
            minute=0,                    # At the top of the hour
        ),
        'options': {
            'expires': 3600 * 12,  # Task expires after 12 hours if not executed
        }
    },
    'download-and-import-gis-data-annually': {
        'task': 'data.tasks_new.download_and_import_gis_data',
        'schedule': crontab(
            month_of_year='1',          # January
            day_of_month='15',          # 15th day of the month
            hour=3,                      # 3 AM
            minute=0,                    # At the top of the hour
        ),
        'options': {
            'expires': 3600 * 24,  # Task expires after 24 hours if not executed
        }
    },
}

# Timezone for the schedule
app.conf.timezone = 'America/Chicago'  # Houston is in Central Time


@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')

