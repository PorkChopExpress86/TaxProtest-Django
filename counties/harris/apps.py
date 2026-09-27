from django.apps import AppConfig


class HarrisConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "counties.harris"
    # The app moved from the top-level ``data`` package into ``counties.harris``.
    # The label is pinned so existing tables (``data_*``), migration history, and
    # content types keep working without a data migration.
    label = "data"
    verbose_name = "Harris County (HCAD)"

    def ready(self):
        # Models are importable only once the app registry is ready.
        from counties.common.candidate_ports import register
        from counties.harris.etl_pipeline.candidate import HarrisCandidatePort

        register("harris", HarrisCandidatePort())
