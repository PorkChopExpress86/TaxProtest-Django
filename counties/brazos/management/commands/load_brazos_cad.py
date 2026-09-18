"""CLI adapter for the Brazos certified CAD source stage."""

from django.core.management.base import BaseCommand

from counties.brazos.annual_refresh import RefreshOptions
from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.property_import import (
    PropertyImportMode,
    PropertyImportRequest,
    build_default_property_import,
)


class Command(BaseCommand):
    help = "Scrape, download, extract, and ingest BCAD certified property data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--year",
            type=int,
            help="Target tax year (e.g., 2024). If omitted, the latest available is used.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-download even if the archive already exists on disk.",
        )
        parser.add_argument(
            "--skip-download",
            action="store_true",
            help="Skip the scrape + download step (use the archive already on disk).",
        )
        parser.add_argument(
            "--skip-extract",
            action="store_true",
            help="Skip the extraction step (use already-extracted files).",
        )
        parser.add_argument(
            "--skip-ingest",
            action="store_true",
            help="Skip the database COPY step (download + extract only).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log every action without writing to disk or the database.",
        )
        parser.add_argument(
            "--keep-extracted",
            action="store_true",
            help="Keep uncompressed extracted data files on disk after loading (default: false, cleans up)",
        )

    def handle(self, *args, **options):
        stage = CadRefreshStage(self)
        refresh_options = RefreshOptions(
            tax_year=options.get("year"),
            force=options["force"],
            skip_download=options["skip_download"],
            skip_extract=options["skip_extract"],
            dry_run=options["dry_run"],
            keep_extracted=options["keep_extracted"],
        )
        if options["skip_ingest"]:
            stage.prepare(refresh_options)
            self.stdout.write("Skipped ingest (--skip-ingest).")
            return

        result = build_default_property_import(self).run(
            PropertyImportRequest(mode=PropertyImportMode.CAD_RECOVERY, options=refresh_options)
        )
        if result.dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[dry-run] Brazos CAD recovery validated for tax_year={result.tax_year}."
                )
            )
            return
        if result.prepared:
            self.stdout.write(
                f"Brazos CAD import {result.workflow_state}; published data unchanged. Candidate {result.candidate_id}; operation {result.operation_id}. Review in Django admin /admin/data/importcandidate/."
            )
            return
        self.stdout.write(
            self.style.WARNING(
                f"Brazos CAD recovery published a Partial property snapshot for "
                f"tax_year={result.tax_year}; year-matched GIS is still unavailable."
            )
        )
