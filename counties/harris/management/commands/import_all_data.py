"""Management command to run complete data import via the modern ETL pipeline.

Usage:
    python manage.py import_all_data [--skip-download] [--skip-property] [--skip-building] [--skip-gis]
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from counties.harris.etl_pipeline import (
    ExtractedSourceRetention,
    HarrisAcquisitionMode,
    HarrisApply,
    HarrisExtractionMode,
    HarrisFailurePolicy,
    HarrisImportPlan,
    HarrisImportRequest,
    run_harris_import,
)


class Command(BaseCommand):
    help = "Run complete data import via the authoritative modern ETL pipeline"

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-download",
            action="store_true",
            help="Skip downloading (use already-downloaded archives)",
        )
        parser.add_argument(
            "--skip-extract",
            action="store_true",
            help="Skip archive extraction (use already-extracted files)",
        )
        parser.add_argument(
            "--skip-property",
            action="store_true",
            help="Skip property records import",
        )
        parser.add_argument(
            "--skip-building",
            action="store_true",
            help="Skip building data import",
        )
        parser.add_argument(
            "--skip-gis",
            action="store_true",
            help="Skip GIS data import",
        )
        parser.add_argument(
            "--skip-contract-validation",
            action="store_true",
            help=(
                "Skip strict post-load completeness validation. Intended for automatic startup "
                "refreshes where the app should still start after a successful data load."
            ),
        )
        parser.add_argument(
            "--keep-extracted",
            action="store_true",
            help="Keep uncompressed extracted data files on disk after loading (default: false, cleans up)",
        )

    def handle(self, *args, **options):
        include_property = not options["skip_property"]
        include_building = not options["skip_building"]
        include_gis = not options["skip_gis"]

        if not any([include_property, include_building, include_gis]):
            raise CommandError("Nothing to import: all import stages were skipped.")

        plan = HarrisImportPlan.from_stage_flags(
            include_property=include_property,
            include_building=include_building,
            include_gis=include_gis,
        )
        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS("COMPLETE DATA IMPORT (MODERN ETL)"))
        self.stdout.write(self.style.SUCCESS("=" * 70))

        request = HarrisImportRequest(
            plan=plan,
            acquisition=(
                HarrisAcquisitionMode.REUSE_DOWNLOADED
                if options["skip_download"]
                else HarrisAcquisitionMode.FETCH
            ),
            extraction=(
                HarrisExtractionMode.REUSE_EXTRACTED
                if options["skip_extract"]
                else HarrisExtractionMode.EXTRACT
            ),
            load=HarrisApply(
                validate_completeness=not options["skip_contract_validation"],
                extracted_source_retention=(
                    ExtractedSourceRetention.RETAIN
                    if options["keep_extracted"]
                    else ExtractedSourceRetention.REMOVE_AFTER_SUCCESS
                ),
            ),
            failure_policy=HarrisFailurePolicy.STRICT,
        )
        result = run_harris_import(request)

        self.stdout.write("")
        self.stdout.write(self.style.WARNING("Pipeline Results:"))
        self.stdout.write(f"  Status: {result.status.value}")
        self.stdout.write(f"  Duration: {result.duration:.1f}s")

        for stage, stage_result in result.stages.items():
            status = self.style.SUCCESS("✓") if stage_result.success else self.style.ERROR("✗")
            self.stdout.write(f"  {status} {stage.value}: {stage_result.duration:.1f}s")
            if stage_result.error:
                self.stdout.write(self.style.ERROR(f"      Error: {stage_result.error}"))

        if result.errors:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR("Errors:"))
            for error in result.errors[:10]:
                self.stdout.write(self.style.ERROR(f"  - {error}"))

        if not result.success:
            raise CommandError("Authoritative modern ETL import failed")

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS("Authoritative modern ETL import completed successfully.")
        )
