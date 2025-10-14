
from .celery import app as celery_app

# Expose Celery app as a package-level variable for `celery -A taxprotest worker`
__all__ = ("celery_app",)
