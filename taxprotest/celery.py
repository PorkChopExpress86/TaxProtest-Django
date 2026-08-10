import os
from pathlib import Path

from celery import Celery
from celery.schedules import crontab

# set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taxprotest.settings")

BASE_DIR = Path(__file__).resolve().parent.parent

app = Celery("taxprotest")

# Beat writes a shelve database next to its schedule. Keep it in the project
# package's runtime directory rather than the repository root, matching how each
# county app keeps its runtime data under counties/<slug>/var/.
app.conf.beat_schedule_filename = os.environ.get(
    "CELERY_BEAT_SCHEDULE_FILENAME",
    str(BASE_DIR / "taxprotest" / "var" / "celerybeat-schedule"),
)

# Keep celery related config under environment variables prefixed with CELERY_
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load task modules from all registered Django app configs.
app.autodiscover_tasks()


# Periodic task schedule
app.conf.beat_schedule = {
    "download-and-import-building-data-monthly": {
        "task": "counties.harris.tasks_new.run_etl_pipeline",
        "kwargs": {
            "scope": "building-only",
            "strict": True,
        },
        "schedule": crontab(
            day_of_week="tuesday",  # Tuesday
            day_of_month="8-14",  # 2nd Tuesday (days 8-14)
            hour=2,  # 2 AM
            minute=0,  # At the top of the hour
        ),
        "options": {
            "expires": 3600 * 12,  # Task expires after 12 hours if not executed
        },
    },
    "download-and-import-gis-data-annually": {
        "task": "counties.harris.tasks_new.run_etl_pipeline",
        "kwargs": {
            "scope": "gis-only",
            "strict": True,
        },
        "schedule": crontab(
            month_of_year="1",  # January
            day_of_month="15",  # 15th day of the month
            hour=3,  # 3 AM
            minute=0,  # At the top of the hour
        ),
        "options": {
            "expires": 3600 * 24,  # Task expires after 24 hours if not executed
        },
    },
}

# Timezone for the schedule
app.conf.timezone = "America/Chicago"  # Houston is in Central Time


@app.task(bind=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
