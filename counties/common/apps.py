from django.apps import AppConfig


class CommonConfig(AppConfig):
    """Registers the shared county web layer so its template tags load.

    Shared durable models retain the ``data`` label and Harris migrations.
    """

    name = "counties.common"
    label = "counties_common"
    verbose_name = "Shared county web layer"
