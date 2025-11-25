# ETL Pipeline Revamp Plan

## Objective
Revamp the entire data ETL (Extract, Transform, Load) pipeline for HCAD property data to improve reliability, performance, and maintainability.

## Current Architecture

### Files Involved
- `data/etl.py` - Core ETL logic for parsing and loading data
- `data/tasks.py` - Celery tasks for scheduled imports
- `data/management/commands/` - Management commands for manual imports
- `data/models.py` - PropertyRecord, BuildingDetail, ExtraFeature models

### Current Process
1. **Download**: HTTP requests to HCAD data URLs
2. **Extract**: ZIP file extraction to temp directory
3. **Transform**: Parse fixed-width text files into Python objects
4. **Load**: Bulk insert into PostgreSQL with batch processing

### Known Issues
- Limited error handling and recovery
- No checksum validation for downloads
- Memory-intensive for large files
- Difficult to resume failed imports
- Limited observability and metrics
- Tightly coupled components

## Proposed Architecture

### 1. Download Manager
**Purpose**: Robust file downloading with retry and validation

**Features**:
- Configurable retry logic with exponential backoff
- SHA256 checksum validation
- Parallel downloads for multiple files
- Progress tracking and ETA calculation
- Bandwidth throttling support
- Resume partial downloads

**URLS for Property data**
Replace {year} with the current year, or latest year. Current year should be updated March/April every year. Data and explainations of the files are located in .github\instructions\database-instructions.instructions.md.
- https://download.hcad.org/data/CAMA/{year}/Real_acct_owner.zip
- https://download.hcad.org/data/CAMA/{year}/Real_acct_ownership_history.zip
- https://download.hcad.org/data/CAMA/{year}/Real_building_land.zip
- https://download.hcad.org/data/CAMA/{year}/Real_jur_exempt.zip
- https://download.hcad.org/data/CAMA/{year}/Code_description_real.zip
- https://download.hcad.org/data/CAMA/{year}/PP_files.zip
- https://download.hcad.org/data/CAMA/{year}/Code_description_pp.zip
- https://download.hcad.org/data/CAMA/{year}/Hearing_files.zip

**URL for GIS Data**
This is the parcel GIS data and should be downloaded to the same location, but will be processed separtly than the Property data.
- https://download.hcad.org/data/GIS/Parcels.zip

**Implementation**:
```python
class DownloadManager:
    def download_file(url, dest_path, checksum=None, max_retries=3)
    def download_batch(file_specs, max_parallel=3)
    def verify_checksum(file_path, expected_hash)
```

**File management**
- All downloaded files are zip files and should be downloaded in the dowloads folder.

### 2. Extract Manager
**Purpose**: Safe and efficient archive extraction

**Features**:
- Support multiple archive formats (ZIP, TAR, GZ)
- Streaming extraction for large files
- Archive integrity validation
- Memory-efficient processing
- Automatic cleanup on errors

**Implementation**:
```python
class ExtractManager:
    def extract_archive(archive_path, dest_dir, validate=True)
    def stream_extract(archive_path, file_filter=None)
    def verify_archive(archive_path)
```

### 3. Transform Pipeline
**Purpose**: Data parsing, validation, and normalization

**Features**:
- Schema-driven field mapping
- Type validation and coercion
- Data quality checks
- Normalization and deduplication
- Error collection and reporting
- Support for multiple source formats

**Implementation**:
```python
class DataTransformer:
    def parse_fixed_width(file_path, schema)
    def validate_record(record, validation_rules)
    def normalize_data(record, normalization_rules)
    def deduplicate(records, key_fields)
```

**File Management**
The files in downloads will be extracted to the folder extracted. Each file will have its own folder with the zip file name that will contain all of the extracted text or data files. Overwrite all files if there are conflicts, or delete folders before extracting again.

### 4. Load Manager
**Purpose**: Efficient and reliable database loading into a PostGres database hosted on docker. 

**Features**:
- Check file encoding type to ensure import sucess
- Drop existing table data to avoid duplicates
- Transaction-safe bulk inserts
- Idempotent operations (upsert support)
- Batch size optimization
- Connection pooling
- Progress checkpointing
- Rollback on errors
- Partial import support
- Options for low memory systems

**Implementation**:
```python
class LoadManager:
    def bulk_upsert(records, model_class, batch_size=5000)
    def checkpoint_progress(import_id, records_processed)
    def resume_from_checkpoint(import_id)
    def rollback_import(import_id)
```

### 5. ETL Orchestrator
**Purpose**: Coordinate all ETL stages with error handling

**Features**:
- Check file encoding type to ensure that the files are properly read
- Stage-by-stage execution
- Dependency management
- Error handling and recovery
- Comprehensive logging
- Metrics collection
- Notification system
- Have option to drop existing data and reimport all data fresh to avoid duplicates

**Implementation**:
```python
class ETLOrchestrator:
    def execute_pipeline(pipeline_config)
    def handle_stage_error(stage, error)
    def collect_metrics()
    def send_notifications(status)
```

## Implementation Phases

### Phase 1: Foundation (Week 1-2) ✅ COMPLETE
- [x] Create base classes for Download, Extract, Transform, Load managers
- [x] Implement configuration system for data sources
- [x] Set up logging infrastructure
- [x] Add basic unit tests

### Phase 2: Download & Extract (Week 3)
- [x] Implement DownloadManager with retry logic
- [x] Add checksum validation
- [x] Implement ExtractManager with streaming support
- [ ] Add integration tests

### Phase 3: Transform (Week 4)
- [x] Refactor data parsing logic into DataTransformer
- [x] Implement validation rules
- [x] Add normalization and deduplication
- [ ] Add transform tests

### Phase 4: Load (Week 5)
- [x] Implement LoadManager with bulk upsert
- [x] Add checkpointing and resume support
- [x] Optimize batch sizes
- [ ] Add load tests

### Phase 5: Orchestration (Week 6)
- [x] Implement ETLOrchestrator
- [x] Add error handling and recovery
- [x] Implement metrics collection
- [ ] Add end-to-end tests

### Phase 6: Migration & Testing (Week 7) ✅ COMPLETE
- [x] Migrate existing imports to new pipeline
- [x] Update Celery tasks (`data/tasks_new.py`)
- [x] Update management commands (`data/management/commands/etl_pipeline.py`)
- [x] Add integration tests (`data/etl_pipeline/tests/test_integration.py`)
- [ ] Performance testing and optimization

### Phase 7: Documentation & Deployment (Week 8)
- [ ] Update documentation
- [ ] Add runbooks for operators
- [ ] Deploy to staging
- [ ] Deploy to production

## Success Criteria

### Reliability
- 99.9% success rate for scheduled imports
- Automatic recovery from transient failures
- Zero data loss on failures

### Performance
- 50% faster import times
- 30% reduction in memory usage
- Support for parallel processing

### Maintainability
- 80%+ test coverage
- Clear separation of concerns
- Comprehensive documentation
- Easy to extend for new data sources

### Observability
- Structured logging for all operations
- Metrics for each stage
- Alerting on failures
- Progress tracking in real-time

## References
- [DATABASE.md](./DATABASE.md) - Current data schema and import process
- [.github/instructions/database-instructions.instructions.md](./.github/instructions/database-instructions.instructions.md) - HCAD data schema
- [data/etl.py](./data/etl.py) - Current ETL implementation
- [data/tasks.py](./data/tasks.py) - Current Celery tasks

## Notes
- This is a major refactor; changes will be incremental and tested
- Backwards compatibility will be maintained during migration
- Old ETL code will be deprecated gradually
- Performance benchmarks will be collected at each phase
