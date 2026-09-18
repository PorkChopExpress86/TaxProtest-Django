from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisApply,
    HarrisExtractionMode,
    HarrisImportPlan,
    HarrisImportRequest,
    run_harris_import,
)
from counties.harris.etl_pipeline.orchestrator import HarrisPropertyFile


class Command(BaseCommand):
    help = "Load HCAD Real Account (real_acct.txt) into PropertyRecord table."

    def add_arguments(self, parser):
        parser.add_argument("--actor", default="")
        parser.add_argument(
            "filepath",
            nargs="?",
            help="Path to real_acct.txt (default: <HCAD_EXTRACT_DIR>/Real_acct_owner/real_acct.txt)",
        )
        parser.add_argument("--chunk", type=int, default=5000, help="Bulk insert chunk size")
        parser.add_argument(
            "--limit", type=int, default=None, help="Limit number of rows to insert (for testing)"
        )
        parser.add_argument(
            "--truncate",
            action="store_true",
            default=True,
            help="Truncate table before import (default: True)",
        )
        parser.add_argument(
            "--no-truncate",
            action="store_true",
            help="Do NOT truncate table before import (append to existing data)",
        )
        parser.add_argument(
            "--no-refresh-readiness",
            action="store_true",
            help="Skip readiness recomputation after property import",
        )

    def handle(self, *args, **options):
        if options.get("filepath"):
            filepath = Path(options["filepath"])
        else:
            filepath = Path(settings.HCAD_EXTRACT_DIR) / "Real_acct_owner" / "real_acct.txt"
        if not filepath.is_absolute():
            filepath = Path(settings.BASE_DIR) / filepath
        if not filepath.exists():
            raise CommandError(f"File not found: {filepath}")
        result = run_harris_import(
            HarrisImportRequest(
                plan=HarrisImportPlan.from_legacy_scope("property-only"),
                acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                load=HarrisApply(
                    refresh_readiness=not options.get("no_refresh_readiness"),
                    validate_completeness=False,
                ),
                property_file=HarrisPropertyFile(
                    path=filepath,
                    append=options.get("no_truncate", False),
                    limit=options.get("limit"),
                    batch_size=options["chunk"],
                ),
                actor=options["actor"],
                origin="command",
            )
        )
        self.stdout.write(
            f"Status: {result.status.value}; applied: {result.wrote_data}; operation: {result.operation_id}; candidate: {result.candidate_id}. Review in Django admin /admin/data/importcandidate/."
        )
        if result.status.value in ("failed", "partial"):
            raise CommandError("Property source import failed: " + "; ".join(result.errors))
