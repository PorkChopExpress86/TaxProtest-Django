"""
Integration tests for ETL Pipeline.

These tests verify the full pipeline flow from download through load.
They use mocking for network operations but test real file operations.
"""

import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from django.core.management.base import CommandError as DjangoCommandError
from django.test import TestCase, override_settings

from data.etl_pipeline import ETLConfig, ETLOrchestrator
from data.etl_pipeline.config import DataSource, DataSourceType, FileFormat
from data.etl_pipeline.download import DownloadManager, DownloadResult
from data.etl_pipeline.extract import ExtractManager, ExtractResult
from data.etl_pipeline.model_loader import ModelLoader
from data.etl_pipeline.transform import DataTransformer, REAL_ACCT_SCHEMA
from data.etl_pipeline.orchestrator import PipelineStatus
from data.models import PropertyRecord, BuildingDetail


class TestETLConfigIntegration(TestCase):
    """Integration tests for ETL configuration."""
    
    def test_config_creates_directories(self):
        """Test that config creates required directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = ETLConfig(
                download_dir=Path(tmpdir) / 'downloads',
                extract_dir=Path(tmpdir) / 'extracted',
                log_dir=Path(tmpdir) / 'logs',
            )
            
            assert config.download_dir.exists()
            assert config.extract_dir.exists()
            assert config.log_dir.exists()
    
    def test_config_from_env_variables(self):
        """Test configuration from environment variables."""
        with patch.dict(os.environ, {
            'ETL_DATA_YEAR': '2024',
            'ETL_DRY_RUN': 'true',
            'ETL_BATCH_SIZE': '1000',
        }):
            config = ETLConfig.from_env()
            
            assert config.data_year == 2024
            assert config.dry_run is True
            assert config.load.batch_size == 1000


class TestDownloadManagerIntegration(TestCase):
    """Integration tests for download manager."""
    
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ETLConfig(
            download_dir=Path(self.tmpdir) / 'downloads',
            extract_dir=Path(self.tmpdir) / 'extracted',
            log_dir=Path(self.tmpdir) / 'logs',
        )
    
    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
    
    @patch('requests.Session.get')
    def test_download_single_file(self, mock_get):
        """Test downloading a single file."""
        # Mock response
        mock_response = Mock()
        mock_response.headers = {'content-length': '1000'}
        mock_response.iter_content.return_value = [b'test data'] * 10
        mock_response.raise_for_status = Mock()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_get.return_value = mock_response
        
        source = DataSource(
            name="Test Source",
            url_template="https://example.com/test.zip",
            filename="test.zip",
            source_type=DataSourceType.PROPERTY_DATA,
        )
        
        manager = DownloadManager(self.config)
        result = manager.download_file(source)
        
        assert result.success
        assert result.bytes_downloaded > 0
        assert result.local_path.exists()
    
    def test_checksum_verification(self):
        """Test checksum verification."""
        # Create a test file
        test_file = self.config.download_dir / 'test.txt'
        test_file.write_text('test content')
        
        manager = DownloadManager(self.config)
        
        # Calculate actual hash
        actual_hash = manager._get_file_hash(test_file)
        
        # Verify correct hash
        assert manager.verify_checksum(test_file, actual_hash)
        
        # Verify incorrect hash fails
        assert not manager.verify_checksum(test_file, 'wrong_hash')


class TestExtractManagerIntegration(TestCase):
    """Integration tests for extract manager."""
    
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ETLConfig(
            download_dir=Path(self.tmpdir) / 'downloads',
            extract_dir=Path(self.tmpdir) / 'extracted',
            log_dir=Path(self.tmpdir) / 'logs',
        )
    
    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
    
    def test_extract_zip_archive(self):
        """Test extracting a ZIP archive."""
        # Create test ZIP
        zip_path = self.config.download_dir / 'test.zip'
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('data.txt', 'test data content')
            zf.writestr('info.txt', 'info content')
        
        source = DataSource(
            name="Test Source",
            url_template="https://example.com/test.zip",
            filename="test.zip",
            source_type=DataSourceType.PROPERTY_DATA,
        )
        
        manager = ExtractManager(self.config)
        result = manager.extract_archive(source)
        
        assert result.success
        assert len(result.files_extracted) == 2
        assert result.extract_dir.exists()
        assert (result.extract_dir / 'data.txt').exists()
    
    def test_extract_with_patterns(self):
        """Test extraction with file patterns."""
        # Create test ZIP with multiple files
        zip_path = self.config.download_dir / 'test.zip'
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('real_acct.txt', 'account data')
            zf.writestr('building_res.txt', 'building data')
            zf.writestr('readme.pdf', 'documentation')
        
        source = DataSource(
            name="Test Source",
            url_template="https://example.com/test.zip",
            filename="test.zip",
            source_type=DataSourceType.PROPERTY_DATA,
            extract_patterns=['*.txt'],
        )
        
        manager = ExtractManager(self.config)
        result = manager.extract_archive(source)
        
        assert result.success
        # PDF should be excluded by allowed_extensions
        assert any('real_acct.txt' in f for f in result.files_extracted)
    
    def test_validate_corrupt_archive(self):
        """Test validation rejects corrupt archives."""
        # Create corrupt ZIP
        corrupt_zip = self.config.download_dir / 'corrupt.zip'
        corrupt_zip.write_text('not a valid zip file')
        
        manager = ExtractManager(self.config)
        assert not manager.verify_archive(corrupt_zip)


class TestDataTransformerIntegration(TestCase):
    """Integration tests for data transformer."""
    
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ETLConfig(
            download_dir=Path(self.tmpdir) / 'downloads',
            extract_dir=Path(self.tmpdir) / 'extracted',
            log_dir=Path(self.tmpdir) / 'logs',
        )
    
    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
    
    def test_transform_csv_file(self):
        """Test transforming a CSV file."""
        # Create test CSV
        csv_path = Path(self.tmpdir) / 'test.csv'
        csv_path.write_text(
            'acct,site_addr_1,zip,tot_appr_val\n'
            '12345,123 Main St,77001,250000\n'
            '67890,456 Oak Ave,77002,350000\n'
        )
        
        transformer = DataTransformer(self.config)
        
        records = list(transformer.iter_records(csv_path, REAL_ACCT_SCHEMA))
        
        assert len(records) == 2
        assert records[0]['account_number'] == '12345'
        assert records[0]['value'] == 250000.0
    
    def test_transform_tab_delimited(self):
        """Test transforming tab-delimited file."""
        # Create test TSV
        tsv_path = Path(self.tmpdir) / 'test.txt'
        tsv_path.write_text(
            'acct\tsite_addr_1\tzip\ttot_appr_val\n'
            '12345\t123 Main St\t77001\t$250,000\n'
        )
        
        transformer = DataTransformer(self.config)
        
        records = list(transformer.iter_records(tsv_path, REAL_ACCT_SCHEMA))
        
        assert len(records) == 1
        assert records[0]['account_number'] == '12345'
        assert records[0]['value'] == 250000.0  # Currency parsed


class TestETLOrchestratorIntegration(TestCase):
    """Integration tests for ETL orchestrator."""
    
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ETLConfig(
            download_dir=Path(self.tmpdir) / 'downloads',
            extract_dir=Path(self.tmpdir) / 'extracted',
            log_dir=Path(self.tmpdir) / 'logs',
            dry_run=True,  # Don't actually modify database
        )
    
    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
    
    def test_orchestrator_initialization(self):
        """Test orchestrator initializes all managers."""
        orchestrator = ETLOrchestrator(self.config)
        
        assert orchestrator.download_manager is not None
        assert orchestrator.extract_manager is not None
        assert orchestrator.transformer is not None
        assert orchestrator.load_manager is not None
    
    @patch('data.etl_pipeline.download.DownloadManager.download_batch')
    @patch('data.etl_pipeline.extract.ExtractManager.extract_batch')
    def test_pipeline_execution_skip_stages(self, mock_extract, mock_download):
        """Test pipeline execution with skipped stages."""
        mock_download.return_value = []
        mock_extract.return_value = []
        
        orchestrator = ETLOrchestrator(self.config)
        
        result = orchestrator.execute(
            skip_download=True,
            skip_extract=True,
            skip_load=True,
        )
        
        # Download and extract should not be called
        mock_download.assert_not_called()
        mock_extract.assert_not_called()
    
    def test_get_status(self):
        """Test getting pipeline status."""
        orchestrator = ETLOrchestrator(self.config)
        
        status = orchestrator.get_status()
        
        assert 'current_stage' in status
        assert 'config' in status
        assert status['config']['data_year'] == self.config.data_year
    
    def test_cleanup(self):
        """Test cleanup removes temporary files."""
        # Create some test files
        (self.config.extract_dir / 'test_folder').mkdir()
        (self.config.extract_dir / 'test_folder' / 'file.txt').write_text('test')
        
        orchestrator = ETLOrchestrator(self.config)
        orchestrator.cleanup(remove_downloads=False, remove_extracts=True)
        
        # Extract dir should be cleaned
        assert not any(self.config.extract_dir.iterdir())

    @patch('data.etl.load_gis_parcels')
    def test_process_gis_source_prefers_parcelscity_from_legacy_extract(self, mock_load_gis):
        """GIS stage should prefer ParcelsCity shapefile when available."""
        mock_load_gis.return_value = 123

        # Modern extracted path contains a fallback Parcels.shp
        modern_extract = self.config.extract_dir / 'Parcels'
        modern_extract.mkdir(parents=True, exist_ok=True)
        (modern_extract / 'Parcels.shp').write_text('stub')

        # Legacy-style extract path contains preferred ParcelsCity.shp
        preferred_dir = (
            self.config.download_dir
            / 'Parcels'
            / 'Parcels'
            / 'Gis'
            / 'pdata'
            / 'ParcelsCity'
        )
        preferred_dir.mkdir(parents=True, exist_ok=True)
        preferred_shp = preferred_dir / 'ParcelsCity.shp'
        preferred_shp.write_text('stub')

        source = DataSource(
            name='GIS Parcels',
            url_template='https://example.com/GIS/Parcels.zip',
            filename='Parcels.zip',
            source_type=DataSourceType.GIS_DATA,
        )

        orchestrator = ETLOrchestrator(self.config)
        result = orchestrator._process_gis_source(source)

        mock_load_gis.assert_called_once_with(str(preferred_shp), refresh_readiness=False)
        assert result['loaded'] == 123
        assert result['failed'] == 0

    def test_transform_load_prefers_extra_feature_detail_files_and_appends(self):
        """When detail feature files exist, skip fallback extra_features.txt."""
        source = DataSource(
            name='Real Building Land',
            url_template='https://example.com/Real_building_land.zip',
            filename='Real_building_land.zip',
            source_type=DataSourceType.PROPERTY_DATA,
        )

        extract_path = self.config.extract_dir / 'Real_building_land'
        extract_path.mkdir(parents=True, exist_ok=True)
        (extract_path / 'building_res.txt').write_text('acct\tbld_num\n')
        (extract_path / 'fixtures.txt').write_text('acct\tbld_num\ttype\tunits\n')
        (extract_path / 'extra_features.txt').write_text('acct\tbld_num\n')
        (extract_path / 'extra_features_detail1.txt').write_text('acct\tbld_num\n')
        (extract_path / 'extra_features_detail2.txt').write_text('acct\tbld_num\n')

        orchestrator = ETLOrchestrator(self.config)

        with patch.object(orchestrator, '_preload_fixtures') as mocked_preload:
            with patch.object(orchestrator, '_process_data_file', return_value={'loaded': 1, 'failed': 0}) as mocked_process:
                result = orchestrator._execute_transform_load([source])

        mocked_preload.assert_called_once_with([source])
        called_files = [call.args[0].stem for call in mocked_process.call_args_list]
        assert called_files == [
            'building_res',
            'extra_features_detail1',
            'extra_features_detail2',
        ]

        truncate_flags = [call.kwargs.get('truncate') for call in mocked_process.call_args_list]
        assert truncate_flags == [True, True, False]
        assert result.success is True
        assert result.metrics['records_loaded'] == 3
        assert result.metrics['records_failed'] == 0
        assert result.metrics['records_invalid'] == 0
        assert result.metrics['records_skipped'] == 0

    def test_missing_required_extract_path_fails_strict_run(self):
        orchestrator = ETLOrchestrator(self.config)
        result = orchestrator.execute(
            scope='full',
            skip_download=True,
            skip_extract=True,
            skip_load=False,
            strict=True,
            validate_contract=False,
        )
        assert result.status == PipelineStatus.FAILED

    @patch('data.etl_pipeline.orchestrator.call_command')
    @patch.object(ETLOrchestrator, '_execute_transform_load')
    def test_validation_failure_propagates_when_strict(self, mocked_transform_load, mocked_call_command):
        self.config.dry_run = False
        mocked_transform_load.return_value = MagicMock(
            success=True,
            completed_at=datetime.now(),
            duration=0.1,
            error=None,
            metrics={'records_loaded': 0, 'records_failed': 0},
            stage='load',
        )
        mocked_call_command.side_effect = DjangoCommandError('validation failed')

        orchestrator = ETLOrchestrator(self.config)
        result = orchestrator.execute(
            scope='full',
            skip_download=True,
            skip_extract=True,
            skip_load=False,
            strict=True,
            validate_contract=True,
        )
        assert result.status == PipelineStatus.FAILED

    def test_skip_load_transform_counts_do_not_materialize_list(self):
        source = DataSource(
            name='Real Account Owner',
            url_template='https://example.com/Real_acct_owner.zip',
            filename='Real_acct_owner.zip',
            source_type=DataSourceType.PROPERTY_DATA,
        )
        extract_path = self.config.extract_dir / 'Real_acct_owner'
        extract_path.mkdir(parents=True, exist_ok=True)
        (extract_path / 'real_acct.txt').write_text('acct\\tstr_num\\tstr\\nA\\t1\\tMAIN\\n')

        orchestrator = ETLOrchestrator(self.config)
        with patch.object(
            orchestrator.transformer,
            'iter_records',
            return_value=iter([{'account_number': 'A'}, {'account_number': 'B'}]),
        ) as mocked_iter:
            result = orchestrator._process_data_file(
                extract_path / 'real_acct.txt',
                skip_load=True,
                truncate=True,
            )

        mocked_iter.assert_called_once()
        assert result == {'loaded': 2, 'invalid': 0, 'skipped': 0, 'failed': 0}

    def test_resolve_schema_name_maps_extra_feature_detail_files(self):
        assert ETLOrchestrator._resolve_schema_name('extra_features_detail1') == 'extra_features'
        assert ETLOrchestrator._resolve_schema_name('extra_features_detail2') == 'extra_features'


class TestEndToEndPipeline(TestCase):
    """End-to-end pipeline tests with mock network."""
    
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ETLConfig(
            download_dir=Path(self.tmpdir) / 'downloads',
            extract_dir=Path(self.tmpdir) / 'extracted',
            log_dir=Path(self.tmpdir) / 'logs',
        )
    
    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
    
    def _create_test_data_zip(self, path: Path):
        """Create a test data ZIP with sample HCAD data."""
        with zipfile.ZipFile(path, 'w') as zf:
            # Create real_acct.txt
            real_acct_data = (
                'acct\tstr_num\tstr\tsite_addr_1\tsite_addr_3\ttot_appr_val\n'
                '1234567890123\t100\tMAIN ST\t100 MAIN ST\t77001\t250000\n'
                '1234567890124\t200\tOAK AVE\t200 OAK AVE\t77002\t350000\n'
            )
            zf.writestr('real_acct.txt', real_acct_data)
            
            # Create building_res.txt
            building_data = (
                'acct\tbld_num\timprv_type\tdate_erected\theat_ar\n'
                '1234567890123\t1\tA1\t2000\t2500\n'
                '1234567890124\t1\tA2\t2010\t1800\n'
            )
            zf.writestr('building_res.txt', building_data)
    
    @patch('requests.Session.get')
    def test_full_download_extract_flow(self, mock_get):
        """Test full download and extract flow."""
        # Create test ZIP
        test_zip_path = Path(self.tmpdir) / 'source.zip'
        self._create_test_data_zip(test_zip_path)
        
        # Mock download to copy our test ZIP
        def mock_download(*args, **kwargs):
            response = Mock()
            response.headers = {'content-length': str(test_zip_path.stat().st_size)}
            response.raise_for_status = Mock()
            
            with open(test_zip_path, 'rb') as f:
                content = f.read()
            
            response.iter_content.return_value = [content]
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            return response
        
        mock_get.side_effect = mock_download
        
        # Create single test source
        test_source = DataSource(
            name="Test Real Acct",
            url_template="https://example.com/test.zip",
            filename="test.zip",
            source_type=DataSourceType.PROPERTY_DATA,
        )
        
        # Download
        download_manager = DownloadManager(self.config)
        download_result = download_manager.download_file(test_source)
        
        assert download_result.success
        assert download_result.local_path.exists()
        
        # Extract
        extract_manager = ExtractManager(self.config)
        extract_result = extract_manager.extract_archive(test_source)
        
        assert extract_result.success
        assert len(extract_result.files_extracted) == 2
        
        # Transform
        transformer = DataTransformer(self.config)
        real_acct_file = extract_result.extract_dir / 'real_acct.txt'
        
        records = list(transformer.iter_records(real_acct_file, REAL_ACCT_SCHEMA))
        
        assert len(records) == 2
        assert records[0]['account_number'] == '1234567890123'


class TestModelLoaderIntegration(TestCase):
    """Integration tests for model loading behavior."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ETLConfig(
            download_dir=Path(self.tmpdir) / 'downloads',
            extract_dir=Path(self.tmpdir) / 'extracted',
            log_dir=Path(self.tmpdir) / 'logs',
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_property_records_skips_non_residential_rows(self):
        loader = ModelLoader(self.config, batch_size=10)
        records = iter(
            [
                {
                    'account_number': 'RES001',
                    'state_class': 'A1',
                    'street_number': '100',
                    'street_name': 'MAIN',
                    'street_suffix': 'ST',
                    'site_addr_1': '100 MAIN ST',
                    'city': 'HOUSTON',
                    'zipcode': '77001',
                    'owner_name': 'OWNER A',
                    'value': 250000,
                    'assessed_value': 240000,
                    'building_area': 2000,
                    'land_area': 5000,
                },
                {
                    'account_number': 'NONRES001',
                    'state_class': 'F1',
                    'street_number': '200',
                    'street_name': 'COMMERCE',
                    'street_suffix': 'ST',
                    'site_addr_1': '200 COMMERCE ST',
                    'city': 'HOUSTON',
                    'zipcode': '77002',
                    'owner_name': 'OWNER B',
                    'value': 750000,
                },
            ]
        )

        result = loader.load_property_records(records, truncate=True, batch_id='test_batch')

        assert result.records_loaded == 1
        assert result.records_skipped == 1
        assert PropertyRecord.objects.count() == 1

        prop = PropertyRecord.objects.get(account_number='RES001')
        assert prop.is_residential is True
        assert prop.state_class == 'A1'

    def test_load_building_details_uses_residential_account_map(self):
        loader = ModelLoader(self.config, batch_size=10)

        residential = PropertyRecord.objects.create(
            account_number='RESB001',
            address='100 MAIN ST',
            city='HOUSTON',
            zipcode='77001',
            state_class='A1',
            is_residential=True,
        )
        PropertyRecord.objects.create(
            account_number='NONRESB001',
            address='200 COMMERCE ST',
            city='HOUSTON',
            zipcode='77002',
            state_class='F1',
            is_residential=False,
        )

        records = iter(
            [
                {
                    'account_number': 'RESB001',
                    'building_number': 1,
                    'building_type': 'A1',
                    'quality_code': 'C',
                    'condition_code': 'AV',
                },
                {
                    'account_number': 'NONRESB001',
                    'building_number': 1,
                    'building_type': 'A1',
                    'quality_code': 'C',
                    'condition_code': 'AV',
                },
            ]
        )

        result = loader.load_building_details(records, truncate=True, batch_id='building_test')

        assert result.records_loaded == 1
        assert result.records_invalid == 1
        assert BuildingDetail.objects.count() == 1

        building = BuildingDetail.objects.get(account_number='RESB001')
        assert building.property_id == residential.id
