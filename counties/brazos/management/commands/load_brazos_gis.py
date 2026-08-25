"""CLI adapter for the Brazos GIS source stage."""

from django.core.management.base import BaseCommand

from counties.brazos.annual_refresh import RefreshOptions
from counties.brazos.gis_refresh import GisRefreshStage


class Command(BaseCommand):
    help = "Download and load BCAD's GIS parcel shapefile into PropertyAccount."

    def add_arguments(self, parser):
        parser.add_argument(
            "--year",
            type=int,
            help="PropertyAccount.tax_year to update. If omitted, the latest tax_year "
            "already present in PropertyAccount is used.",
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
            help="Skip the extraction step (use an already-extracted shapefile).",
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
        GisRefreshStage(self).run(
            RefreshOptions(
                tax_year=options.get("year"),
                force=options["force"],
                skip_download=options["skip_download"],
                skip_extract=options["skip_extract"],
                dry_run=options["dry_run"],
                keep_extracted=options["keep_extracted"],
            )
        )
