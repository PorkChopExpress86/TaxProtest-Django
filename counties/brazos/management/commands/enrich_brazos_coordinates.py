"""CLI adapter for deliberately stale, coordinate-only BCAD enrichment."""

from django.core.management.base import BaseCommand

from counties.brazos.annual_refresh import RefreshOptions
from counties.brazos.coordinate_enrichment import (
    BrazosCoordinateEnrichment,
    CoordinateEnrichmentReport,
)


class Command(BaseCommand):
    help = (
        "Measure, then optionally apply, coordinates from an earlier BCAD certified GIS "
        "release to a later CAD year. This never creates an annual snapshot."
    )

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, required=True, help="CAD tax year to enrich.")
        parser.add_argument(
            "--source-year",
            type=int,
            help="Expected GIS source year. Required only when selecting a retained offline source.",
        )
        parser.add_argument("--force", action="store_true", help="Re-download the GIS archive.")
        parser.add_argument(
            "--skip-download", action="store_true", help="Use a retained GIS archive."
        )
        parser.add_argument(
            "--skip-extract", action="store_true", help="Use retained extracted GIS files."
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the measured coordinate-only update after its safety gate passes.",
        )
        parser.add_argument(
            "--minimum-match-rate",
            type=float,
            help="Explicit target-CAD match-rate threshold required with --apply (0 to 1).",
        )
        parser.add_argument(
            "--keep-extracted",
            action="store_true",
            help="Keep GIS extraction after a successful apply (analysis always retains it for review).",
        )

    def handle(self, *args, **options):
        enrichment = BrazosCoordinateEnrichment(reporter=self)
        preparation, report, candidates = enrichment.analyze(
            RefreshOptions(
                tax_year=options["year"],
                source_year=options.get("source_year"),
                force=options["force"],
                skip_download=options["skip_download"],
                skip_extract=options["skip_extract"],
                keep_extracted=options["keep_extracted"],
            )
        )
        self._write_report(report)
        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING("Analysis complete; no database rows were changed.")
            )
            return

        updated = enrichment.apply(
            report,
            candidates,
            minimum_match_rate=options.get("minimum_match_rate"),
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Coordinate enrichment complete: {updated} PropertyAccount rows updated "
                f"from BCAD GIS source year {report.source_year}."
            )
        )
        if not options["keep_extracted"] and not options["skip_extract"]:
            enrichment.cleanup(preparation)

    def _write_report(self, report: CoordinateEnrichmentReport) -> None:
        self.stdout.write(
            "Coordinate coverage: "
            f"source_year={report.source_year}; target_year={report.target_year}; "
            f"source_records={report.source_records}; usable_coordinates={report.usable_coordinate_records}; "
            f"distinct_source_ids={report.distinct_source_ids}; duplicate_source_ids={report.duplicate_source_ids}; "
            f"invalid_coordinate_records={report.invalid_coordinate_records}; "
            f"target_accounts={report.target_accounts}; matched_accounts={report.matched_accounts}; "
            f"unmatched_target_accounts={report.unmatched_target_accounts}; "
            f"unmatched_source_ids={report.unmatched_source_ids}; match_rate={report.match_rate:.4f}"
        )
