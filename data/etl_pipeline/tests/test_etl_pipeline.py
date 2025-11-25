"""
Unit tests for ETL Pipeline components.
"""

import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from data.etl_pipeline.config import (
    ETLConfig, DataSource, DataSourceType, FileFormat,
    DownloadConfig, LoadConfig, RetryConfig
)
from data.etl_pipeline.logging import ETLLogger, ETLMetrics
from data.etl_pipeline.download import DownloadManager, DownloadResult
from data.etl_pipeline.extract import ExtractManager, ExtractResult
from data.etl_pipeline.transform import (
    DataTransformer, FieldSchema, TableSchema, 
    TransformResult, ValidationError
)


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
            'data_year': 2024,
            'dry_run': True,
            'load': {
                'batch_size': 1000,
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
        
        assert 'data_year' in data
        assert 'download_dir' in data
        assert 'sources' in data


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
        assert metrics.errors[0]['message'] == "Test error"
        assert metrics.errors[0]['context'] == {"field": "value"}
    
    def test_to_dict(self):
        """Test serialization to dictionary."""
        metrics = ETLMetrics()
        metrics.records_processed = 100
        metrics.records_success = 90
        metrics.records_failed = 10
        
        data = metrics.to_dict()
        
        assert data['records_processed'] == 100
        assert data['records_success'] == 90
        assert data['records_failed'] == 10
        assert 'duration_seconds' in data


class TestETLLogger:
    """Tests for ETL logger."""
    
    def test_logger_creation(self):
        """Test creating logger."""
        logger = ETLLogger(name='test_logger', log_to_file=False)
        
        assert logger.name == 'test_logger'
        assert logger.logger is not None
    
    def test_stage_context_manager(self):
        """Test stage context manager."""
        logger = ETLLogger(name='test_logger', log_to_file=False)
        
        with logger.stage('test_stage') as metrics:
            metrics.records_processed = 50
        
        assert 'test_stage' in logger.metrics
        assert logger.metrics['test_stage'].records_processed == 50


class TestFieldSchema:
    """Tests for field schema."""
    
    def test_get_source_name_match(self):
        """Test finding matching source field."""
        schema = FieldSchema(
            name='account_number',
            source_names=['acct', 'account', 'account_number'],
        )
        
        available = ['ACCT', 'value', 'other']
        result = schema.get_source_name(available)
        
        assert result == 'ACCT'
    
    def test_get_source_name_no_match(self):
        """Test when no matching field found."""
        schema = FieldSchema(
            name='account_number',
            source_names=['acct', 'account'],
        )
        
        available = ['value', 'other']
        result = schema.get_source_name(available)
        
        assert result is None


class TestTableSchema:
    """Tests for table schema."""
    
    def test_get_field_by_name(self):
        """Test getting field by name."""
        schema = TableSchema(
            name='test',
            fields=[
                FieldSchema('field1', ['f1']),
                FieldSchema('field2', ['f2']),
            ],
        )
        
        field = schema.get_field('field1')
        assert field is not None
        assert field.name == 'field1'
    
    def test_get_field_names(self):
        """Test getting all field names."""
        schema = TableSchema(
            name='test',
            fields=[
                FieldSchema('field1', ['f1']),
                FieldSchema('field2', ['f2']),
            ],
        )
        
        names = schema.get_field_names()
        assert names == ['field1', 'field2']


class TestDataTransformer:
    """Tests for data transformer."""
    
    def test_detect_encoding(self):
        """Test encoding detection."""
        config = ETLConfig()
        transformer = DataTransformer(config)
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("test,data,here\n")
            f.write("1,2,3\n")
            temp_path = Path(f.name)
        
        try:
            encoding = transformer._detect_encoding(temp_path)
            assert encoding in ['utf-8', 'latin-1', 'cp1252']
        finally:
            temp_path.unlink()
    
    def test_sniff_delimiter_comma(self):
        """Test delimiter detection for CSV."""
        config = ETLConfig()
        transformer = DataTransformer(config)
        
        sample = "a,b,c\n1,2,3\n4,5,6"
        delimiter = transformer._sniff_delimiter(sample)
        assert delimiter == ','
    
    def test_sniff_delimiter_tab(self):
        """Test delimiter detection for TSV."""
        config = ETLConfig()
        transformer = DataTransformer(config)
        
        sample = "a\tb\tc\n1\t2\t3\n4\t5\t6"
        delimiter = transformer._sniff_delimiter(sample)
        assert delimiter == '\t'
    
    def test_coerce_value_int(self):
        """Test integer coercion."""
        config = ETLConfig()
        transformer = DataTransformer(config)
        
        schema = FieldSchema('test', ['test'], field_type='int')
        
        assert transformer._coerce_value('123', schema) == 123
        assert transformer._coerce_value('123.0', schema) == 123
        assert transformer._coerce_value('', schema) is None
    
    def test_coerce_value_decimal(self):
        """Test decimal coercion with currency."""
        config = ETLConfig()
        transformer = DataTransformer(config)
        
        schema = FieldSchema('test', ['test'], field_type='decimal')
        
        assert transformer._coerce_value('$1,234.56', schema) == 1234.56
        assert transformer._coerce_value('1000', schema) == 1000.0
    
    def test_transform_row(self):
        """Test row transformation."""
        config = ETLConfig()
        transformer = DataTransformer(config)
        
        schema = TableSchema(
            name='test',
            fields=[
                FieldSchema('id', ['ID'], field_type='int'),
                FieldSchema('name', ['NAME'], field_type='str', max_length=10),
            ],
        )
        
        row = {'ID': '123', 'NAME': '  Test Name Here  '}
        result, errors = transformer.transform_row(row, schema)
        
        assert result['id'] == 123
        assert result['name'] == 'Test Name'  # Truncated to 10 chars
        assert len(errors) == 0


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
        assert path.name == 'test_data'
    
    def test_validate_zip_valid(self):
        """Test ZIP validation with valid archive."""
        config = ETLConfig()
        manager = ExtractManager(config)
        
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            temp_path = Path(f.name)
        
        try:
            # Create valid ZIP
            with zipfile.ZipFile(temp_path, 'w') as zf:
                zf.writestr('test.txt', 'test content')
            
            assert manager._validate_zip(temp_path) is True
        finally:
            temp_path.unlink()
    
    def test_validate_zip_invalid(self):
        """Test ZIP validation with invalid archive."""
        config = ETLConfig()
        manager = ExtractManager(config)
        
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as f:
            f.write(b'not a valid zip file')
            temp_path = Path(f.name)
        
        try:
            assert manager._validate_zip(temp_path) is False
        finally:
            temp_path.unlink()
    
    def test_should_extract_file_allowed(self):
        """Test file filtering by extension."""
        config = ETLConfig()
        manager = ExtractManager(config)
        
        assert manager._should_extract_file('data.txt') is True
        assert manager._should_extract_file('data.csv') is True
        assert manager._should_extract_file('data.exe') is False
    
    def test_should_extract_file_patterns(self):
        """Test file filtering by patterns."""
        config = ETLConfig()
        manager = ExtractManager(config)
        
        patterns = ['real_*.txt', 'building_*.txt']
        
        assert manager._should_extract_file('real_acct.txt', patterns) is True
        assert manager._should_extract_file('building_res.txt', patterns) is True
        assert manager._should_extract_file('other.txt', patterns) is False


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
            files_extracted=['file1.txt', 'file2.txt'],
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
