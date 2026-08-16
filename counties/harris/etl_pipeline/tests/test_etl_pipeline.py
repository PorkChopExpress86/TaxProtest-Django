"""
Unit tests for ETL Pipeline components.
"""

import os
import tempfile
import unittest
import zipfile
from datetime import datetime
from email.utils import formatdate
from pathlib import Path
from unittest.mock import MagicMock

from counties.harris.etl_pipeline.config import (
    DataSource,
    DataSourceType,
    ETLConfig,
    LoadConfig,
    RetryConfig,
)
from counties.harris.etl_pipeline.download import DownloadManager, DownloadResult
from counties.harris.etl_pipeline.extract import ExtractManager, ExtractResult
from counties.harris.etl_pipeline.logging import ETLLogger, ETLMetrics


class TestETLConfig:
    """Tests for ETL configuration."""

    def test_default_config_creation(self):
        """Test creating config with defaults."""
        config = ETLConfig()

        assert config.data_year == datetime.now().year
        assert config.dry_run is False
        assert len(config.property_sources) > 0
        assert len(config.gis_sources) > 0

    def test_data_source_url_generation(self):
        """Test URL generation with year placeholder."""
        source = DataSource(
            name="Test Source",
            url_template="https://example.com/{year}/data.zip",
            filename="data.zip",
            source_type=DataSourceType.PROPERTY_DATA,
        )

        assert source.get_url(2025) == "https://example.com/2025/data.zip"
        assert source.get_url(2024) == "https://example.com/2024/data.zip"

    def test_config_from_dict(self):
        """Test creating config from dictionary."""
        data = {
            "data_year": 2024,
            "dry_run": True,
            "load": {
                "batch_size": 1000,
            },
        }

        config = ETLConfig.from_dict(data)

        assert config.data_year == 2024
        assert config.dry_run is True
        assert config.load.batch_size == 1000

    def test_get_all_sources_sorted_by_priority(self):
        """Test that sources are sorted by priority."""
        config = ETLConfig()
        sources = config.get_all_sources()

        priorities = [s.priority for s in sources]
        assert priorities == sorted(priorities)

    def test_get_required_sources(self):
        """Test filtering for required sources only."""
        config = ETLConfig()
        required = config.get_required_sources()

        assert all(s.required for s in required)

    def test_config_to_dict(self):
        """Test serialization to dictionary."""
        config = ETLConfig()
        data = config.to_dict()

        assert "data_year" in data
        assert "download_dir" in data
        assert "sources" in data


class TestETLMetrics:
    """Tests for ETL metrics collection."""

    def test_metrics_duration(self):
        """Test duration calculation."""
        metrics = ETLMetrics()
        metrics.records_processed = 100
        metrics.records_success = 95

        # Should have some non-zero duration
        assert metrics.duration >= 0

    def test_success_rate_calculation(self):
        """Test success rate calculation."""
        metrics = ETLMetrics()
        metrics.records_processed = 100
        metrics.records_success = 80

        assert metrics.success_rate == 80.0

    def test_success_rate_zero_records(self):
        """Test success rate with no records."""
        metrics = ETLMetrics()
        assert metrics.success_rate == 0.0

    def test_add_error(self):
        """Test adding errors to metrics."""
        metrics = ETLMetrics()
        metrics.add_error("Test error", {"field": "value"})

        assert len(metrics.errors) == 1
        assert metrics.errors[0]["message"] == "Test error"
        assert metrics.errors[0]["context"] == {"field": "value"}

    def test_to_dict(self):
        """Test serialization to dictionary."""
        metrics = ETLMetrics()
        metrics.records_processed = 100
        metrics.records_success = 90
        metrics.records_failed = 10

        data = metrics.to_dict()

        assert data["records_processed"] == 100
        assert data["records_success"] == 90
        assert data["records_failed"] == 10
        assert "duration_seconds" in data


class TestETLLogger:
    """Tests for ETL logger."""

    def test_logger_creation(self):
        """Test creating logger."""
        logger = ETLLogger(name="test_logger", log_to_file=False)

        assert logger.name == "test_logger"
        assert logger.logger is not None

    def test_stage_context_manager(self):
        """Test stage context manager."""
        logger = ETLLogger(name="test_logger", log_to_file=False)

        with logger.stage("test_stage") as metrics:
            metrics.records_processed = 50

        assert "test_stage" in logger.metrics
        assert logger.metrics["test_stage"].records_processed == 50


class TestExtractManager:
    """Tests for extract manager."""

    def test_extract_path_generation(self):
        """Test extraction path generation."""
        config = ETLConfig()
        manager = ExtractManager(config)

        source = DataSource(
            name="Test",
            url_template="http://example.com/test.zip",
            filename="test_data.zip",
            source_type=DataSourceType.PROPERTY_DATA,
        )

        path = manager._get_extract_path(source)
        assert path.name == "test_data"

    def test_validate_zip_valid(self):
        """Test ZIP validation with valid archive."""
        config = ETLConfig()
        manager = ExtractManager(config)

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            temp_path = Path(f.name)

        try:
            # Create valid ZIP
            with zipfile.ZipFile(temp_path, "w") as zf:
                zf.writestr("test.txt", "test content")

            assert manager._validate_zip(temp_path) is True
        finally:
            temp_path.unlink()

    def test_validate_zip_invalid(self):
        """Test ZIP validation with invalid archive."""
        config = ETLConfig()
        manager = ExtractManager(config)

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(b"not a valid zip file")
            temp_path = Path(f.name)

        try:
            assert manager._validate_zip(temp_path) is False
        finally:
            temp_path.unlink()

    def test_should_extract_file_allowed(self):
        """Test file filtering by extension."""
        config = ETLConfig()
        manager = ExtractManager(config)

        assert manager._should_extract_file("data.txt") is True
        assert manager._should_extract_file("data.csv") is True
        assert manager._should_extract_file("data.exe") is False

    def test_should_extract_file_patterns(self):
        """Test file filtering by patterns."""
        config = ETLConfig()
        manager = ExtractManager(config)

        patterns = ["real_*.txt", "building_*.txt"]

        assert manager._should_extract_file("real_acct.txt", patterns) is True
        assert manager._should_extract_file("building_res.txt", patterns) is True
        assert manager._should_extract_file("other.txt", patterns) is False


class TestDownloadResult:
    """Tests for download result."""

    def test_download_result_str(self):
        """Test string representation."""
        source = DataSource(
            name="Test",
            url_template="http://example.com/test.zip",
            filename="test.zip",
            source_type=DataSourceType.PROPERTY_DATA,
        )

        result = DownloadResult(source=source, success=True)
        assert "SUCCESS" in str(result)

        result = DownloadResult(source=source, success=False)
        assert "FAILED" in str(result)


class TestExtractResult:
    """Tests for extract result."""

    def test_extract_result_str(self):
        """Test string representation."""
        source = DataSource(
            name="Test",
            url_template="http://example.com/test.zip",
            filename="test.zip",
            source_type=DataSourceType.PROPERTY_DATA,
        )

        result = ExtractResult(
            source=source,
            success=True,
            files_extracted=["file1.txt", "file2.txt"],
        )
        assert "SUCCESS" in str(result)
        assert "2 files" in str(result)


class TestRetryConfig:
    """Tests for retry configuration."""

    def test_default_retry_config(self):
        """Test default retry values."""
        config = RetryConfig()

        assert config.max_retries == 3
        assert config.initial_delay == 1.0
        assert config.max_delay == 60.0
        assert config.exponential_base == 2.0
        assert config.jitter is True


class TestLoadConfig:
    """Tests for load configuration."""

    def test_default_load_config(self):
        """Test default load values."""
        config = LoadConfig()

        assert config.batch_size == 5000
        assert config.use_transactions is True
        assert config.truncate_before_load is True
        assert config.low_memory_mode is False


class TestConditionalDownload(unittest.TestCase):
    """Tests for skip-if-unchanged download behavior."""

    @staticmethod
    def _manager(tmp: str) -> DownloadManager:
        config = ETLConfig(
            download_dir=Path(tmp),
            extract_dir=Path(tmp) / "extracted",
            log_dir=Path(tmp) / "logs",
        )
        return DownloadManager(config)

    @staticmethod
    def _source() -> DataSource:
        return DataSource(
            name="Test",
            url_template="https://example.com/test.zip",
            filename="test.zip",
            source_type=DataSourceType.PROPERTY_DATA,
        )

    def test_parse_last_modified_roundtrip(self):
        ts = 1_700_000_000
        parsed = DownloadManager._parse_last_modified(formatdate(ts, usegmt=True))
        self.assertIsNotNone(parsed)
        self.assertLess(abs(parsed - ts), 1.0)

    def test_parse_last_modified_invalid(self):
        self.assertIsNone(DownloadManager._parse_last_modified("not a date"))
        self.assertIsNone(DownloadManager._parse_last_modified(None))

    def test_skips_download_when_size_and_mtime_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            source = self._source()

            dest = Path(tmp) / "test.zip"
            dest.write_bytes(b"x" * 100)
            ts = 1_700_000_000
            os.utime(dest, (ts, ts))

            manager.session = MagicMock()
            head = MagicMock()
            head.status_code = 200
            head.headers = {"content-length": "100", "last-modified": formatdate(ts, usegmt=True)}
            manager.session.head.return_value = head

            result = manager.download_file(source)

            self.assertTrue(result.success)
            self.assertEqual(result.attempts, 0)  # served from local cache
            manager.session.get.assert_not_called()

    def test_downloads_when_remote_size_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            source = self._source()

            dest = Path(tmp) / "test.zip"
            dest.write_bytes(b"x" * 100)
            ts = 1_700_000_000
            os.utime(dest, (ts, ts))

            manager.session = MagicMock()
            head = MagicMock()
            head.status_code = 200
            head.headers = {"content-length": "999", "last-modified": formatdate(ts, usegmt=True)}
            manager.session.head.return_value = head

            # Stub the actual transfer so the test never touches the network.
            sentinel = DownloadResult(source=source, success=True, attempts=1)
            manager._download_with_progress = MagicMock(return_value=sentinel)

            result = manager.download_file(source)

            self.assertEqual(result.attempts, 1)  # fell through to a real download
            manager._download_with_progress.assert_called_once()

    def test_force_download_env_disables_skip(self):
        os.environ["ETL_FORCE_DOWNLOAD"] = "1"
        try:
            cfg = ETLConfig.from_env()
            self.assertFalse(cfg.download.skip_if_unchanged)
        finally:
            del os.environ["ETL_FORCE_DOWNLOAD"]
