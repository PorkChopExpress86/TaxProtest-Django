from django.apps import AppConfig


class BrazosConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "counties.brazos"
    # Pinned for the same reason as Harris: the package moved, the app label
    # (and therefore ``brazos_cad_*`` tables and migration history) did not.
    label = "brazos_cad"
    verbose_name = "Brazos County (BCAD)"
