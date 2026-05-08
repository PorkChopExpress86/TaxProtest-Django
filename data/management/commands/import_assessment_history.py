from __future__ import annotations

from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from data.assessment_history import AssessmentHistoryImporter
from data.etl_pipeline import DownloadManager, ETLConfig, ExtractManager
from data.etl_pipeline.config import DataSource


class Command(BaseCommand):
    help = "Import multi-year assessed value history from HCAD Real Account and Hearing files"

    def add_arguments(self, parser):
        current_year = datetime.now().year
        parser.add_argument("--start-year", type=int, default=current_year - 4)
        parser.add_argument("--end-year", type=int, default=current_year)
        parser.add_argument("--skip-download", action="store_true")
        parser.add_argument("--skip-extract", action="store_true")
        parser.add_argument(
            "--download-root",
            type=str,
            default=str(Path(settings.HCAD_DOWNLOAD_DIR) / "assessment_history"),
        )
        parser.add_argument(
            "--extract-root",
            type=str,
            default=str(Path(settings.HCAD_EXTRACT_DIR) / "assessment_history"),
        )

    def handle(self, *args, **options):
        start_year = options["start_year"]
        end_year = options["end_year"]
        if start_year > end_year:
            raise CommandError("--start-year must be less than or equal to --end-year")

        download_root = Path(options["download_root"])
        extract_root = Path(options["extract_root"])
        skip_download = options["skip_download"]
        skip_extract = options["skip_extract"]

        years = list(range(start_year, end_year + 1))
        for year in years:
            self._prepare_year_data(
                year=year,
                download_root=download_root,
                extract_root=extract_root,
                skip_download=skip_download,
                skip_extract=skip_extract,
            )

        importer = AssessmentHistoryImporter()
        counts = importer.import_year_range(start_year, end_year, extract_root)

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {counts.records_loaded} assessment history rows for {counts.years_processed} year(s)."
            )
        )

    def _prepare_year_data(
        self,
        *,
        year: int,
        download_root: Path,
        extract_root: Path,
        skip_download: bool,
        skip_extract: bool,
    ) -> None:
        config = ETLConfig.from_env()
        config.data_year = year
        config.download_dir = download_root / str(year)
        config.extract_dir = extract_root / str(year)
        config.log_dir = Path(settings.HCAD_LOG_DIR) / "assessment_history" / str(year)
        config.download_dir.mkdir(parents=True, exist_ok=True)
        config.extract_dir.mkdir(parents=True, exist_ok=True)
        config.log_dir.mkdir(parents=True, exist_ok=True)

        download_manager = DownloadManager(config)
        extract_manager = ExtractManager(config)
        sources = self._history_sources(config)

        if not skip_download:
            results = download_manager.download_batch(sources, max_parallel=1)
            failed_required = [
                result
                for result in results
                if not result.success and result.source.name == "Real Account Owner"
            ]
            if failed_required:
                raise CommandError(f"Failed to download Real Account Owner for {year}")

        if not skip_extract:
            to_extract = [source for source in sources if download_manager.is_downloaded(source)]
            results = extract_manager.extract_batch(to_extract)
            failed_required = [
                result
                for result in results
                if not result.success and result.source.name == "Real Account Owner"
            ]
            if failed_required:
                raise CommandError(f"Failed to extract Real Account Owner for {year}")

    @staticmethod
    def _history_sources(config: ETLConfig) -> list[DataSource]:
        sources: list[DataSource] = []
        for name in ("Real Account Owner", "Hearing Files"):
            source = config.get_source_by_name(name)
            if source is None:
                raise CommandError(f"Missing ETL source definition: {name}")
            sources.append(source)
        return sources
