from django.apps import AppConfig


class CommonConfig(AppConfig):
    """Registers the shared county web layer so its template tags load.

    Holds no models — it exists in ``INSTALLED_APPS`` only so Django discovers
    ``counties/common/templatetags/``.
    """

    name = "counties.common"
    label = "counties_common"
    verbose_name = "Shared county web layer"
