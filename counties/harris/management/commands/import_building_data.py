"""Compatibility command for building imports through the Harris ETL pipeline."""

from django.core.management.base import BaseCommand

from counties.harris.etl_pipeline import HarrisImportPlan
from counties.harris.tasks_new import _run_authoritative_pipeline, run_etl_pipeline


class Command(BaseCommand):
    help = "Import Harris building data through the authoritative translated-row pipeline"

    def add_arguments(self, parser):
        parser.add_argument(
            "--async",
            action="store_true",
            help="Run the import asynchronously via Celery (default is synchronous)",
        )
        parser.add_argument(
            "--skip-download",
            action="store_true",
            help="Skip downloading and extracting; use existing files",
        )
        parser.add_argument(
            "--with-gis",
            action="store_true",
            help="Also import GIS coordinate data after the building import",
        )
        parser.add_argument(
            "--no-refresh-readiness",
            action="store_true",
            help="Skip readiness recomputation during building/GIS stages",
        )

    def handle(self, *args, **options):
        plan = HarrisImportPlan.from_stage_flags(
            include_property=False,
            include_building=True,
            include_gis=options["with_gis"],
        )
        skip_download = options["skip_download"]
        refresh_readiness = not options["no_refresh_readiness"]

        if options["async"]:
            self.stdout.write(self.style.SUCCESS("Sending authoritative import to Celery..."))
            task = run_etl_pipeline.delay(
                skip_download=skip_download,
                skip_extract=skip_download,
                scope=plan.legacy_scope,
                strict=True,
                refresh_readiness=refresh_readiness,
                validate_contract=refresh_readiness,
            )
            self.stdout.write(self.style.SUCCESS("Task queued successfully!"))
            self.stdout.write(self.style.SUCCESS(f"Task ID: {task.id}"))
            self.stdout.write(self.style.SUCCESS("Monitor with: docker compose logs -f worker"))
            return

        self.stdout.write(self.style.SUCCESS("Starting authoritative building data import..."))
        result = _run_authoritative_pipeline(
            task_instance=None,
            skip_download=skip_download,
            skip_extract=skip_download,
            skip_load=False,
            data_year=None,
            strict=True,
            plan=plan,
            refresh_readiness=refresh_readiness,
            validate_contract=refresh_readiness,
        )
        self.stdout.write(
            self.style.SUCCESS(f"Authoritative building import completed ({result['status']}).")
        )
