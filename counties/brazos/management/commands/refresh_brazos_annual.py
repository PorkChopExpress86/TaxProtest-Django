"""Publish a complete, year-matched Brazos property snapshot."""

from django.core.management.base import BaseCommand

from counties.brazos.annual_refresh import RefreshOptions, build_default_refresh


class Command(BaseCommand):
    """Coordinate certified CAD replacement and GIS enrichment atomically."""

    help = (
        "Refresh Brazos CAD and GIS data for one source-matched year as an "
        "atomic property snapshot."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--year",
            type=int,
            help="Tax year to refresh. Defaults to the year discovered from the CAD archive.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-download both source archives even when local copies exist.",
        )
        parser.add_argument(
            "--skip-download",
            action="store_true",
            help="Use retained local source archives instead of downloading either source.",
        )
        parser.add_argument(
            "--skip-extract",
            action="store_true",
            help="Use retained extracted CAD and GIS source files.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Select and validate both source snapshots without database writes or cleanup.",
        )
        parser.add_argument(
            "--keep-extracted",
            action="store_true",
            help="Retain both extracted source directories after a successful refresh.",
        )

    def handle(self, *args, **options):
        result = build_default_refresh(self).run(
            RefreshOptions(
                tax_year=options.get("year"),
                force=options["force"],
                skip_download=options["skip_download"],
                skip_extract=options["skip_extract"],
                dry_run=options["dry_run"],
                keep_extracted=options["keep_extracted"],
            )
        )
        if result.dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[dry-run] Brazos annual refresh validated for tax_year={result.tax_year}."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Brazos annual refresh complete for tax_year={result.tax_year}: "
                f"CAD {sum(result.cad.metrics.values())} rows, "
                f"GIS {result.gis.metrics.get('matched', 0)} rows enriched."
            )
        )
