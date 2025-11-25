"""
ETL Pipeline Transform Module

Provides data parsing, validation, and normalization for HCAD data files.
Supports schema-driven field mapping and data quality checks.
"""

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Generator, Iterable, List, Optional, Tuple, Type

from .config import TransformConfig, ETLConfig
from .logging import ETLLogger


# Increase CSV field size limit to handle large HCAD fields
csv.field_size_limit(10485760)  # 10MB limit


@dataclass
class FieldSchema:
    """Schema definition for a single field."""
    name: str
    source_names: List[str]  # Possible column names in source data
    field_type: str = 'str'  # str, int, float, decimal, bool, date
    max_length: Optional[int] = None
    required: bool = False
    default: Any = None
    nullable: bool = True
    validators: List[Callable[[Any], bool]] = field(default_factory=list)
    transform: Optional[Callable[[Any], Any]] = None
    
    def get_source_name(self, available_fields: List[str]) -> Optional[str]:
        """Find matching source field name."""
        available_lower = {f.lower(): f for f in available_fields}
        for name in self.source_names:
            if name.lower() in available_lower:
                return available_lower[name.lower()]
        return None


@dataclass
class TableSchema:
    """Schema definition for a table/file."""
    name: str
    fields: List[FieldSchema]
    key_fields: List[str] = field(default_factory=list)
    
    def get_field(self, name: str) -> Optional[FieldSchema]:
        """Get a field by name."""
        for f in self.fields:
            if f.name == name:
                return f
        return None
    
    def get_field_names(self) -> List[str]:
        """Get all field names."""
        return [f.name for f in self.fields]


@dataclass
class ValidationError:
    """A single validation error."""
    field: str
    value: Any
    message: str
    row_number: Optional[int] = None


@dataclass
class TransformResult:
    """Result of a transform operation."""
    success: bool
    records_processed: int = 0
    records_valid: int = 0
    records_invalid: int = 0
    records_skipped: int = 0
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    
    @property
    def success_rate(self) -> float:
        if self.records_processed > 0:
            return self.records_valid / self.records_processed * 100
        return 0.0


class DataTransformer:
    """Transforms raw data into validated, normalized records.
    
    Features:
    - Schema-driven field mapping
    - Type validation and coercion
    - Data quality checks
    - Normalization and deduplication
    - Error collection and reporting
    - Support for multiple source formats
    """
    
    def __init__(
        self,
        config: ETLConfig,
        logger: Optional[ETLLogger] = None,
    ):
        self.config = config
        self.transform_config = config.transform
        self.logger = logger or ETLLogger(name='data_transformer')
    
    def _detect_encoding(self, filepath: Path) -> str:
        """Detect file encoding."""
        for encoding in self.transform_config.encoding_fallbacks:
            try:
                with open(filepath, 'r', encoding=encoding) as f:
                    f.read(8192)
                return encoding
            except (UnicodeDecodeError, UnicodeError):
                continue
        return 'latin-1'  # Final fallback
    
    def _sniff_delimiter(self, sample: str) -> str:
        """Detect CSV delimiter."""
        candidates = ['\t', '|', ',']
        counts = {d: sample.count(d) for d in candidates}
        # Prefer tab/pipe over comma if tied
        return max(counts, key=lambda d: (counts[d], 1 if d in ('\t', '|') else 0))
    
    def open_reader(
        self,
        filepath: Path,
        encoding: Optional[str] = None,
        delimiter: Optional[str] = None,
    ) -> Tuple[csv.DictReader, Any]:
        """Open a file and return a DictReader.
        
        Returns:
            Tuple of (DictReader, file_handle) - caller must close file_handle
        """
        if encoding is None:
            encoding = self._detect_encoding(filepath)
        
        # Read sample for delimiter detection
        with open(filepath, 'rb') as f:
            sample_bytes = f.read(4096)
        
        try:
            sample = sample_bytes.decode(encoding, errors='ignore')
        except Exception:
            sample = sample_bytes.decode('latin-1', errors='ignore')
            encoding = 'latin-1'
        
        if delimiter is None:
            delimiter = self._sniff_delimiter(sample)
        
        self.logger.debug(
            f"Opening {filepath.name} with encoding={encoding}, delimiter={repr(delimiter)}"
        )
        
        f = open(filepath, 'r', encoding=encoding, errors='ignore', newline='')
        reader = csv.DictReader(f, delimiter=delimiter)
        return reader, f
    
    def _coerce_value(
        self,
        value: Any,
        field_schema: FieldSchema,
    ) -> Any:
        """Coerce a value to the expected type."""
        if value is None or value == '':
            if field_schema.nullable:
                return field_schema.default
            return field_schema.default
        
        # Strip whitespace if configured
        if isinstance(value, str):
            if self.transform_config.strip_fields:
                value = value.strip()
            if self.transform_config.normalize_whitespace:
                value = ' '.join(value.split())
        
        # Type coercion
        try:
            if field_schema.field_type == 'int':
                # Handle float strings like "123.00"
                return int(float(value))
            elif field_schema.field_type == 'float':
                return float(value)
            elif field_schema.field_type == 'decimal':
                # Remove currency symbols and commas
                if isinstance(value, str):
                    value = value.replace('$', '').replace(',', '')
                return float(value)
            elif field_schema.field_type == 'bool':
                if isinstance(value, bool):
                    return value
                return value.lower() in ('true', '1', 'yes', 'y', 't')
            elif field_schema.field_type == 'str':
                result = str(value)
                if field_schema.max_length:
                    result = result[:field_schema.max_length]
                return result
        except (ValueError, TypeError, AttributeError):
            return field_schema.default
        
        return value
    
    def _apply_transform(
        self,
        value: Any,
        field_schema: FieldSchema,
    ) -> Any:
        """Apply custom transform function if defined."""
        if field_schema.transform:
            try:
                return field_schema.transform(value)
            except Exception:
                return value
        return value
    
    def _validate_field(
        self,
        value: Any,
        field_schema: FieldSchema,
    ) -> List[str]:
        """Validate a field value and return list of errors."""
        errors = []
        
        # Check required
        if field_schema.required and (value is None or value == ''):
            errors.append(f"Required field is empty")
            return errors
        
        # Run validators
        for validator in field_schema.validators:
            try:
                if not validator(value):
                    errors.append(f"Validation failed")
            except Exception as e:
                errors.append(f"Validator error: {e}")
        
        return errors
    
    def transform_row(
        self,
        row: Dict[str, Any],
        schema: TableSchema,
        row_number: Optional[int] = None,
    ) -> Tuple[Optional[Dict[str, Any]], List[ValidationError]]:
        """Transform a single row according to schema.
        
        Returns:
            Tuple of (transformed_record, validation_errors)
        """
        result = {}
        errors = []
        available_fields = list(row.keys())
        
        for field_schema in schema.fields:
            source_name = field_schema.get_source_name(available_fields)
            raw_value = row.get(source_name) if source_name else None
            
            # Coerce type
            value = self._coerce_value(raw_value, field_schema)
            
            # Apply custom transform
            value = self._apply_transform(value, field_schema)
            
            # Validate
            field_errors = self._validate_field(value, field_schema)
            for err in field_errors:
                errors.append(ValidationError(
                    field=field_schema.name,
                    value=raw_value,
                    message=err,
                    row_number=row_number,
                ))
            
            result[field_schema.name] = value
        
        return result, errors
    
    def transform_file(
        self,
        filepath: Path,
        schema: TableSchema,
        limit: Optional[int] = None,
    ) -> Generator[Tuple[Dict[str, Any], List[ValidationError]], None, TransformResult]:
        """Transform all rows in a file.
        
        Yields:
            Tuple of (transformed_record, validation_errors) for each row
        
        Returns:
            TransformResult with statistics
        """
        result = TransformResult(success=True)
        
        reader, fh = self.open_reader(filepath)
        
        try:
            for row_num, row in enumerate(reader, start=1):
                result.records_processed += 1
                
                # Transform row
                record, errors = self.transform_row(row, schema, row_num)
                
                if errors:
                    result.records_invalid += 1
                    result.errors.extend(errors[:10])  # Limit stored errors
                    
                    if not self.transform_config.skip_invalid_records:
                        result.success = False
                        break
                    
                    if len(result.errors) >= self.transform_config.max_errors_before_abort:
                        result.success = False
                        result.warnings.append(
                            f"Aborted after {result.records_invalid} invalid records"
                        )
                        break
                else:
                    result.records_valid += 1
                
                yield record, errors
                
                if limit and result.records_processed >= limit:
                    break
                
                # Progress logging
                if row_num % 10000 == 0:
                    self.logger.log_progress(
                        row_num, limit or row_num,
                        stage='transform',
                    )
        finally:
            fh.close()
        
        return result
    
    def iter_records(
        self,
        filepath: Path,
        schema: TableSchema,
        skip_invalid: bool = True,
        limit: Optional[int] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """Iterate over transformed records from a file.
        
        Args:
            filepath: Path to source file
            schema: Schema to use for transformation
            skip_invalid: Skip records with validation errors
            limit: Maximum records to process
        
        Yields:
            Transformed record dictionaries
        """
        reader, fh = self.open_reader(filepath)
        processed = 0
        
        try:
            for row_num, row in enumerate(reader, start=1):
                record, errors = self.transform_row(row, schema, row_num)
                
                if errors and skip_invalid:
                    continue
                
                yield record
                processed += 1
                
                if limit and processed >= limit:
                    break
        finally:
            fh.close()
    
    def deduplicate(
        self,
        records: Iterable[Dict[str, Any]],
        key_fields: List[str],
    ) -> Generator[Dict[str, Any], None, None]:
        """Deduplicate records by key fields.
        
        Args:
            records: Iterable of record dictionaries
            key_fields: Fields to use as unique key
        
        Yields:
            Unique records
        """
        seen = set()
        
        for record in records:
            key = tuple(record.get(f) for f in key_fields)
            if key not in seen:
                seen.add(key)
                yield record


# ============================================================================
# Pre-defined schemas for HCAD data files
# ============================================================================

REAL_ACCT_SCHEMA = TableSchema(
    name='real_acct',
    fields=[
        FieldSchema('account_number', ['acct', 'account', 'account_number'], 'str', max_length=20, required=True),
        FieldSchema('owner_name', ['mailto', 'owner_name', 'owner'], 'str', max_length=255),
        FieldSchema('street_number', ['str_num', 'site_addr_num'], 'str', max_length=16),
        FieldSchema('street_name', ['str', 'site_addr_street'], 'str', max_length=128),
        FieldSchema('site_addr_1', ['site_addr_1', 'site_addr'], 'str', max_length=255),
        FieldSchema('city', ['site_addr_2', 'situs_city', 'city'], 'str', max_length=100),
        FieldSchema('zipcode', ['site_addr_3', 'zip', 'zip_code'], 'str', max_length=10),
        FieldSchema('value', ['tot_appr_val', 'mkt_val'], 'decimal'),
        FieldSchema('assessed_value', ['assessed_val'], 'decimal'),
        FieldSchema('building_area', ['bld_ar', 'bldg_ar'], 'decimal'),
        FieldSchema('land_area', ['land_ar'], 'decimal'),
    ],
    key_fields=['account_number'],
)

BUILDING_RES_SCHEMA = TableSchema(
    name='building_res',
    fields=[
        FieldSchema('account_number', ['acct'], 'str', max_length=20, required=True),
        FieldSchema('building_number', ['bld_num'], 'int'),
        FieldSchema('building_type', ['imprv_type'], 'str', max_length=10),
        FieldSchema('building_style', ['building_style_code'], 'str', max_length=10),
        FieldSchema('building_class', ['bldg_class'], 'str', max_length=10),
        FieldSchema('quality_code', ['qa_cd'], 'str', max_length=10),
        FieldSchema('condition_code', ['cndtn_cd'], 'str', max_length=10),
        FieldSchema('year_built', ['date_erected'], 'int'),
        FieldSchema('year_remodeled', ['yr_remodel'], 'int'),
        FieldSchema('effective_year', ['eff_yr'], 'int'),
        FieldSchema('heat_area', ['heat_ar'], 'decimal'),
        FieldSchema('base_area', ['base_ar'], 'decimal'),
        FieldSchema('gross_area', ['gross_ar'], 'decimal'),
        FieldSchema('stories', ['sty'], 'decimal'),
        FieldSchema('foundation_type', ['foundation'], 'str', max_length=10),
        FieldSchema('exterior_wall', ['exterior_wall'], 'str', max_length=10),
        FieldSchema('roof_cover', ['roof_cover'], 'str', max_length=10),
        FieldSchema('roof_type', ['roof_typ'], 'str', max_length=10),
        FieldSchema('bedrooms', ['bed_rm'], 'int'),
        FieldSchema('full_baths', ['full_bath'], 'int'),
        FieldSchema('half_baths', ['half_bath'], 'int'),
        FieldSchema('fireplaces', ['fireplace'], 'int'),
    ],
    key_fields=['account_number', 'building_number'],
)

EXTRA_FEATURES_SCHEMA = TableSchema(
    name='extra_features',
    fields=[
        FieldSchema('account_number', ['acct'], 'str', max_length=20, required=True),
        FieldSchema('feature_number', ['bld_num'], 'int'),
        FieldSchema('feature_code', ['code'], 'str', max_length=10),
        FieldSchema('feature_description', ['dscr'], 'str', max_length=255),
        FieldSchema('quantity', ['units'], 'decimal'),
        FieldSchema('area', ['area'], 'decimal'),
        FieldSchema('length', ['length'], 'decimal'),
        FieldSchema('width', ['width'], 'decimal'),
        FieldSchema('quality_code', ['grade_cd'], 'str', max_length=10),
        FieldSchema('condition_code', ['cond_cd'], 'str', max_length=10),
        FieldSchema('year_built', ['yr_built'], 'int'),
        FieldSchema('value', ['value'], 'decimal'),
    ],
    key_fields=['account_number', 'feature_number', 'feature_code'],
)

# Schema registry
SCHEMAS = {
    'real_acct': REAL_ACCT_SCHEMA,
    'building_res': BUILDING_RES_SCHEMA,
    'extra_features': EXTRA_FEATURES_SCHEMA,
}


def get_schema(name: str) -> Optional[TableSchema]:
    """Get a schema by name."""
    return SCHEMAS.get(name)
