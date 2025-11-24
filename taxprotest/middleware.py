import logging
import random
import time

from django.conf import settings


class RequestLoggingMiddleware:
    """Log high-level request metadata with configurable sampling."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.logger = logging.getLogger("taxprotest.request")
        self.sample_rate = float(getattr(settings, "REQUEST_LOG_SAMPLE", 1.0) or 0)
        self.static_url = getattr(settings, "STATIC_URL", "/static/")

    def __call__(self, request):
        start = time.perf_counter()
        response = self.get_response(request)
        duration_ms = (time.perf_counter() - start) * 1000

        if self._should_log(request.path):
            user = getattr(request, "user", None)
            user_repr = "anonymous"
            if user and user.is_authenticated:
                user_repr = str(user)

            ip = request.META.get("REMOTE_ADDR", "-")
            ua = request.META.get("HTTP_USER_AGENT", "-")

            self.logger.info(
                "method=%s path=%s status=%s duration_ms=%.2f user=%s ip=%s ua=%s",
                request.method,
                request.get_full_path(),
                getattr(response, "status_code", "-"),
                duration_ms,
                user_repr,
                ip,
                ua,
            )

        return response

    def _should_log(self, path: str) -> bool:
        if self.sample_rate <= 0:
            return False
        if self.sample_rate < 1.0 and random.random() > self.sample_rate:
            return False
        if self.static_url and path.startswith(self.static_url):
            return False
        return True