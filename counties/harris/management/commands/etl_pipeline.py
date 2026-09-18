"""
Management command for ETL pipeline operations.

Usage:
    python manage.py etl_pipeline --help
    python manage.py etl_pipeline download
    python manage.py etl_pipeline extract
    python manage.py etl_pipeline run
    python manage.py etl_pipeline status
"""

import json
import os
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from counties.harris.etl_pipeline import (
    DownloadManager,
    ETLConfig,
    ExtractManager,
    HarrisAcquisitionMode,
    HarrisApply,
    HarrisExtractionMode,
    HarrisFailurePolicy,
    HarrisImportPlan,
    HarrisImportRequest,
    HarrisPreview,
    run_harris_import,
)
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG


class Command(BaseCommand):
    help = "Run ETL pipeline operations for HCAD data import"

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="command", help="ETL command to run")

        # Download command
        download_parser = subparsers.add_parser("download", help="Download HCAD data files")
        download_parser.add_argument(
            "--all",
            action="store_true",
            help="Download all sources including optional ones",
        )
        download_parser.add_argument(
            "--source",
            type=str,
            help="Download a specific source by name",
        )
        download_parser.add_argument(
            "--year",
            type=int,
            help="Data year (default: current year)",
        )

        # Extract command
        extract_parser = subparsers.add_parser("extract", help="Extract downloaded archives")
        extract_parser.add_argument(
            "--source",
            type=str,
            help="Extract a specific source by name",
        )
        extract_parser.add_argument(
            "--validate",
            action="store_true",
            default=True,
            help="Validate archives before extraction (default: True)",
        )

        # Run command (full pipeline)
        run_parser = subparsers.add_parser("run", help="Run the full ETL pipeline")
        run_parser.add_argument(
            "--skip-download",
            action="store_true",
            help="Skip download stage (use existing files)",
        )
        run_parser.add_argument(
            "--skip-extract",
            action="store_true",
            help="Skip extract stage (use existing extracted files)",
        )
        run_parser.add_argument(
            "--skip-load",
            action="store_true",
            help="Skip load stage (dry run for transform)",
        )
        run_parser.add_argument(
            "--year",
            type=int,
            help="Data year (default: current year)",
        )
        run_parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Transform-only feasibility run (no database writes or readiness refresh)",
        )
        run_parser.add_argument(
            "--scope",
            choices=["full", "building-only", "gis-only", "property-only"],
            default="full",
            help="Execution scope (default: full)",
        )
        strict_group = run_parser.add_mutually_exclusive_group()
        strict_group.add_argument(
            "--strict",
            dest="strict",
            action="store_true",
            default=True,
            help="Fail the run on required source or completeness contract gaps (default)",
        )
        strict_group.add_argument(
            "--allow-partial",
            dest="strict",
            action="store_false",
            help="Allow partial completion for debugging",
        )
        run_parser.add_argument(
            "--property-only",
            action="store_true",
            help="Deprecated alias for --scope property-only",
        )
        run_parser.add_argument(
            "--gis-only",
            action="store_true",
            help="Deprecated alias for --scope gis-only",
        )

        # Status command
        status_parser = subparsers.add_parser("status", help="Show pipeline status")
        status_parser.add_argument(
            "--json",
            action="store_true",
            help="Output status as JSON",
        )

        # Cleanup command
        cleanup_parser = subparsers.add_parser("cleanup", help="Clean up temporary files")
        cleanup_parser.add_argument(
            "--downloads",
            action="store_true",
            help="Remove downloaded ZIP files",
        )
        cleanup_parser.add_argument(
            "--extracts",
            action="store_true",
            default=True,
            help="Remove extracted files (default: True)",
        )

        # List command
        list_parser = subparsers.add_parser("list", help="List available data sources")

    def handle(self, *args, **options):
        command = options.get("command")

        if not command:
            self.print_help("manage.py", "etl_pipeline")
            return

        # Build configuration
        config = ETLConfig.from_env()

        # Route to appropriate handler
        if command == "download":
            self.handle_download(config, options)
        elif command == "extract":
            self.handle_extract(config, options)
        elif command == "run":
            self.handle_run(config, options)
        elif command == "status":
            self.handle_status(config, options)
        elif command == "cleanup":
            self.handle_cleanup(config, options)
        elif command == "list":
            self.handle_list(config, options)
        else:
            raise CommandError(f"Unknown command: {command}")

    def handle_download(self, config: ETLConfig, options: dict):
        """Handle download command."""
        data_year = self._data_year(options)
        self.stdout.write(self.style.WARNING(f"Starting download (year={data_year})..."))

        manager = DownloadManager(config, data_year=data_year)

        # Determine which sources to download
        if options.get("source"):
            source = self._source_by_name(options["source"])
            if not source:
                raise CommandError(f"Unknown source: {options['source']}")
            sources = [source]
        elif options.get("all"):
            sources = DEFAULT_HCAD_SOURCE_CATALOG.ordered_sources()
        else:
            sources = DEFAULT_HCAD_SOURCE_CATALOG.required_sources()

        self.stdout.write(f"Downloading {len(sources)} source(s)...")

        results = manager.download_batch(sources)

        # Report results
        success = sum(1 for r in results if r.success)
        failed = len(results) - success
        total_bytes = sum(r.bytes_downloaded for r in results)

        for result in results:
            if result.success:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  ✓ {result.source.name}: {result.bytes_downloaded:,} bytes"
                    )
                )
            else:
                self.stdout.write(self.style.ERROR(f"  ✗ {result.source.name}: {result.error}"))

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Download complete: {success}/{len(results)} succeeded, "
                f"{total_bytes:,} bytes total"
            )
        )

        if failed > 0:
            raise CommandError(f"{failed} download(s) failed")

    def handle_extract(self, config: ETLConfig, options: dict):
        """Handle extract command."""
        self.stdout.write(self.style.WARNING("Starting extraction..."))

        manager = ExtractManager(config)

        # Determine which sources to extract
        if options.get("source"):
            source = self._source_by_name(options["source"])
            if not source:
                raise CommandError(f"Unknown source: {options['source']}")
            sources = [source]
        else:
            sources = DEFAULT_HCAD_SOURCE_CATALOG.ordered_sources()

        # Filter to downloaded sources only
        download_manager = DownloadManager(config, data_year=self._data_year(options))
        sources = [s for s in sources if download_manager.is_downloaded(s)]

        if not sources:
            raise CommandError("No downloaded archives found. Run download first.")

        self.stdout.write(f"Extracting {len(sources)} archive(s)...")

        results = manager.extract_batch(sources)

        # Report results
        success = sum(1 for r in results if r.success)
        total_files = sum(len(r.files_extracted) for r in results)

        for result in results:
            if result.success:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  ✓ {result.source.name}: {len(result.files_extracted)} files"
                    )
                )
            else:
                self.stdout.write(self.style.ERROR(f"  ✗ {result.source.name}: {result.error}"))

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Extraction complete: {success}/{len(results)} succeeded, "
                f"{total_files} files extracted"
            )
        )

    def handle_run(self, config: ETLConfig, options: dict):
        """Handle run command (full pipeline)."""
        scope = options.get("scope", "full")
        if options.get("property_only"):
            scope = "property-only"
        elif options.get("gis_only"):
            scope = "gis-only"
        plan = HarrisImportPlan.from_legacy_scope(scope)

        data_year = self._data_year(options)
        preview = bool(options.get("skip_load") or options.get("dry_run"))
        self.stdout.write(
            self.style.WARNING(
                f"Starting ETL pipeline (year={data_year}, "
                f"dry_run={preview}, plan={plan.legacy_scope}, "
                f'strict={options.get("strict", True)})...'
            )
        )

        request = HarrisImportRequest(
            plan=plan,
            data_year=data_year,
            acquisition=(
                HarrisAcquisitionMode.REUSE_DOWNLOADED
                if options.get("skip_download")
                else HarrisAcquisitionMode.FETCH
            ),
            extraction=(
                HarrisExtractionMode.REUSE_EXTRACTED
                if options.get("skip_extract")
                else HarrisExtractionMode.EXTRACT
            ),
            load=HarrisPreview() if preview else HarrisApply(),
            failure_policy=(
                HarrisFailurePolicy.STRICT
                if options.get("strict", True)
                else HarrisFailurePolicy.BEST_EFFORT
            ),
        )
        result = run_harris_import(request)

        # Report results
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
            for error in result.errors[:5]:
                self.stdout.write(self.style.ERROR(f"  - {error}"))

        if result.status.value in ("prepared", "awaiting_review", "blocked"):
            self.stdout.write(
                self.style.WARNING(
                    f"Published data unchanged. Candidate {result.candidate_id}; operation {result.operation_id}. Review in Django admin /admin/data/importcandidate/."
                )
            )
            return
        if options.get("strict", True):
            if not result.success:
                raise CommandError("Pipeline execution failed")
        elif result.status.value == "failed":
            raise CommandError("Pipeline execution failed")

        self.stdout.write("")
        if result.status.value == "partial":
            self.stdout.write(self.style.WARNING("Pipeline completed with partial results."))
        else:
            self.stdout.write(self.style.SUCCESS("Pipeline completed successfully!"))

    def handle_status(self, config: ETLConfig, options: dict):
        """Handle status command."""
        data_year = self._data_year(options)
        download_manager = DownloadManager(config, data_year=data_year)
        extract_manager = ExtractManager(config)

        status = {
            "config": {
                "data_year": data_year,
                "download_dir": str(config.download_dir),
                "extract_dir": str(config.extract_dir),
            },
            "sources": [],
        }

        for source in DEFAULT_HCAD_SOURCE_CATALOG.ordered_sources():
            source_status = {
                "name": source.name,
                "required": source.required,
                "downloaded": download_manager.is_downloaded(source),
                "extracted": extract_manager.is_extracted(source),
            }
            status["sources"].append(source_status)

        if options.get("json"):
            self.stdout.write(json.dumps(status, indent=2))
        else:
            self.stdout.write(self.style.WARNING("ETL Pipeline Status"))
            self.stdout.write(f"  Data Year: {data_year}")
            self.stdout.write(f"  Download Dir: {config.download_dir}")
            self.stdout.write(f"  Extract Dir: {config.extract_dir}")
            self.stdout.write("")
            self.stdout.write("Sources:")

            for s in status["sources"]:
                downloaded = self.style.SUCCESS("✓") if s["downloaded"] else self.style.ERROR("✗")
                extracted = self.style.SUCCESS("✓") if s["extracted"] else self.style.ERROR("✗")
                required = "*" if s["required"] else " "
                self.stdout.write(
                    f'  {required} {s["name"]}: downloaded={downloaded} extracted={extracted}'
                )

    def handle_cleanup(self, config: ETLConfig, options: dict):
        """Handle cleanup command."""
        self.stdout.write(self.style.WARNING("Cleaning up..."))

        sources = DEFAULT_HCAD_SOURCE_CATALOG.ordered_sources()
        if options.get("downloads", False):
            DownloadManager(config, data_year=self._data_year(options)).cleanup(sources=sources)
        if options.get("extracts", True):
            ExtractManager(config).cleanup(sources=sources)

        self.stdout.write(self.style.SUCCESS("Cleanup complete!"))

    def handle_list(self, config: ETLConfig, options: dict):
        """Handle list command."""
        self.stdout.write(self.style.WARNING("Available Data Sources:"))
        self.stdout.write("")

        data_year = self._data_year(options)
        for source in DEFAULT_HCAD_SOURCE_CATALOG.ordered_sources():
            required = self.style.SUCCESS("[required]") if source.required else "[optional]"
            self.stdout.write(f"  {source.name} {required}")
            self.stdout.write(f"    Type: {source.source_type.value}")
            self.stdout.write(f"    URL: {source.get_url(data_year)}")
            self.stdout.write(f"    Filename: {source.filename}")
            self.stdout.write("")

    @staticmethod
    def _data_year(options: dict) -> int:
        configured = os.getenv("ETL_DATA_YEAR")
        return int(options.get("year") or configured or datetime.now().year)

    @staticmethod
    def _source_by_name(name: str):
        normalized = name.lower()
        return next(
            (
                source
                for source in DEFAULT_HCAD_SOURCE_CATALOG.ordered_sources()
                if source.name.lower() == normalized
            ),
            None,
        )
