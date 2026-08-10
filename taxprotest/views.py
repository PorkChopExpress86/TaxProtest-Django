"""Site-wide views.

Property search, comparables, and protest analysis are not here — every county
renders those from :mod:`counties.common.views` via its own adapter. What is
left is the handful of pages that belong to the site rather than to a county.
"""

import redis
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render


def about(request):
    return render(request, "about.html")


def healthz(request):
    """Return 200 if the app can reach the database."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - defensive
        return JsonResponse({"status": "error", "detail": str(exc)}, status=503)

    return JsonResponse({"status": "ok"})


def readiness(request):
    """Return readiness status including Redis availability."""
    payload = {"database": "ok", "redis": "ok"}
    status_code = 200

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception as exc:
        payload["database"] = "error"
        payload["detail_database"] = str(exc)
        status_code = 503

    try:
        client = redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=1)
        client.ping()
        client.close()
    except Exception as exc:  # pragma: no cover - depends on runtime redis
        payload["redis"] = "error"
        payload["detail_redis"] = str(exc)
        status_code = 503

    return JsonResponse(payload, status=status_code)
