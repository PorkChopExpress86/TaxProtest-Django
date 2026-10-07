from django.apps import AppConfig


class BrazosConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "counties.brazos"
    # Pinned for the same reason as Harris: the package moved, the app label
    # (and therefore ``brazos_cad_*`` tables and migration history) did not.
    label = "brazos_cad"
    verbose_name = "Brazos County (BCAD)"

    def ready(self):
        # Models are importable only once the app registry is ready.
        from counties.brazos.candidate import BrazosCandidatePort, source_roots
        from counties.common.county_registry import CountyRegistration, register

        register(
            CountyRegistration(
                slug="brazos",
                port=BrazosCandidatePort(),
                writer_lock_key=742102,  # every running process must agree; never change
                warning_logger="brazos_cad",
                source_roots=source_roots,
            )
        )
