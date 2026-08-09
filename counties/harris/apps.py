from django.apps import AppConfig


class HarrisConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "counties.harris"
    # The app moved from the top-level ``data`` package into ``counties.harris``.
    # The label is pinned so existing tables (``data_*``), migration history, and
    # content types keep working without a data migration.
    label = "data"
    verbose_name = "Harris County (HCAD)"
