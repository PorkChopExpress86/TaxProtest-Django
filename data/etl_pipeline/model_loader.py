"""
ETL Pipeline Model Loader

Provides the bridge between the generic ETL pipeline and Django models.
Handles loading transformed records into PropertyRecord, BuildingDetail, and ExtraFeature.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Generator, List, Optional, Set, Type
from pathlib import Path

from django.db import connection, transaction
from django.db.models import Model
from django.utils import timezone

from .config import ETLConfig
from .logging import ETLLogger
from .fixtures_aggregator import FixturesAggregator

logger = logging.getLogger(__name__)


@dataclass
class ModelLoadResult:
    """Result of loading records into a Django model."""
    model_name: str
    records_loaded: int = 0
    records_invalid: int = 0
    records_skipped: int = 0
    batch_id: str = ""
    duration: float = 0.0
    error: Optional[str] = None
    
    @property
    def success(self) -> bool:
        return self.error is None
    
    @property
    def total_processed(self) -> int:
        return self.records_loaded + self.records_invalid + self.records_skipped


class ModelLoader:
    """Loads transformed records into Django models.
    
    Handles:
    - PropertyRecord loading from real_acct.txt
    - BuildingDetail loading from building_res.txt
    - ExtraFeature loading from extra_features.txt
    """
    
    def __init__(
        self,
        config: ETLConfig,
        etl_logger: Optional[ETLLogger] = None,
        batch_size: int = 5000,
    ):
        self.config = config
        self.logger = etl_logger or ETLLogger(name='model_loader')
        self.batch_size = batch_size
        self._valid_accounts: Optional[Set[str]] = None
        self._account_to_property: Optional[Dict[str, int]] = None
        self.fixtures_aggregator = FixturesAggregator()
    
    def reset_cache(self) -> None:
        """Clear cached account lookups. Call after loading PropertyRecords."""
        self._valid_accounts = None
        self._account_to_property = None
    
    def _generate_batch_id(self) -> str:
        """Generate a unique batch ID for tracking imports."""
        return timezone.now().strftime("%Y%m%d_%H%M%S")
    
    def _get_valid_accounts(self) -> Set[str]:
        """Get set of valid account numbers from PropertyRecord."""
        if self._valid_accounts is None:
            from data.models import PropertyRecord
            self._valid_accounts = set(
                PropertyRecord.objects.values_list("account_number", flat=True)
            )
            self.logger.info(f"Loaded {len(self._valid_accounts)} valid account numbers")
        return self._valid_accounts
    
    def _get_account_to_property_map(self) -> Dict[str, int]:
        """Get mapping of account numbers to PropertyRecord IDs."""
        if self._account_to_property is None:
            from data.models import PropertyRecord
            self._account_to_property = dict(
                PropertyRecord.objects.values_list("account_number", "id")
            )
            self.logger.info(f"Loaded {len(self._account_to_property)} account->property mappings")
        return self._account_to_property
    
    def _truncate_table(self, model_class: Type[Model]) -> None:
        """Truncate a table for clean import."""
        table_name = model_class._meta.db_table
        self.logger.info(f"Truncating table {table_name}...")
        with connection.cursor() as cursor:
            cursor.execute(f'TRUNCATE TABLE "{table_name}" RESTART IDENTITY CASCADE')
        self.logger.info(f"Table {table_name} truncated successfully")
    
    def load_building_details(
        self,
        records: Generator[Dict[str, Any], None, None],
        truncate: bool = True,
        batch_id: Optional[str] = None,
    ) -> ModelLoadResult:
        """Load BuildingDetail records from transformed data.
        
        Args:
            records: Generator of transformed records
            truncate: Whether to truncate the table before loading
            batch_id: Optional batch identifier
        
        Returns:
            ModelLoadResult with statistics
        """
        from data.models import BuildingDetail, PropertyRecord
        
        start_time = datetime.now()
        batch_id = batch_id or self._generate_batch_id()
        import_date = timezone.now()
        
        result = ModelLoadResult(
            model_name='BuildingDetail',
            batch_id=batch_id,
        )
        
        try:
            valid_accounts = self._get_valid_accounts()
            account_map = self._get_account_to_property_map()
            
            if truncate:
                self._truncate_table(BuildingDetail)
            
            buf: List[BuildingDetail] = []
            
            with transaction.atomic():
                for record in records:
                    acct = record.get('account_number', '').strip()
                    if not acct:
                        result.records_skipped += 1
                        continue
                    
                    if acct not in valid_accounts:
                        result.records_invalid += 1
                        continue
                    
                    # Get property ID
                    property_id = account_map.get(acct)
                    if not property_id:
                        result.records_invalid += 1
                        continue
                    
                    building = BuildingDetail(
                        property_id=property_id,
                        account_number=acct,
                        building_number=self._safe_int(record.get('building_number')),
                        building_type=str(record.get('building_type', ''))[:10],
                        building_style=str(record.get('building_style', ''))[:10],
                        building_class=str(record.get('building_class', ''))[:10],
                        quality_code=str(record.get('quality_code', ''))[:10],
                        condition_code=str(record.get('condition_code', ''))[:10],
                        year_built=self._safe_int(record.get('year_built')),
                        year_remodeled=self._safe_int(record.get('year_remodeled')),
                        effective_year=self._safe_int(record.get('effective_year')),
                        heat_area=self._safe_decimal(record.get('heat_area')),
                        base_area=self._safe_decimal(record.get('base_area')),
                        gross_area=self._safe_decimal(record.get('gross_area')),
                        stories=self._safe_decimal(record.get('stories')),
                        foundation_type=str(record.get('foundation_type', ''))[:10],
                        exterior_wall=str(record.get('exterior_wall', ''))[:10],
                        roof_cover=str(record.get('roof_cover', ''))[:10],
                        roof_type=str(record.get('roof_type', ''))[:10],
                        bedrooms=self._get_bedrooms(account_num, building_num, record),
                        bathrooms=self._get_bathrooms(account_num, building_num, record),
                        half_baths=self._get_half_baths(account_num, building_num, record),
                        fireplaces=self._safe_int(record.get('fireplaces')),
                        is_active=True,
                        import_date=import_date,
                        import_batch_id=batch_id,
                    )
                    
                    buf.append(building)
                    
                    if len(buf) >= self.batch_size:
                        BuildingDetail.objects.bulk_create(buf, ignore_conflicts=True)
                        result.records_loaded += len(buf)
                        self.logger.info(
                            f"Loaded {result.records_loaded} building records "
                            f"(invalid: {result.records_invalid}, skipped: {result.records_skipped})"
                        )
                        buf.clear()
                
                # Load remaining records
                if buf:
                    BuildingDetail.objects.bulk_create(buf, ignore_conflicts=True)
                    result.records_loaded += len(buf)
            
            self.logger.info(f"Completed: Loaded {result.records_loaded} building detail records")
            
        except Exception as e:
            result.error = str(e)
            self.logger.exception(f"Error loading building details: {e}")
        
        result.duration = (datetime.now() - start_time).total_seconds()
        return result
    
    def load_extra_features(
        self,
        records: Generator[Dict[str, Any], None, None],
        truncate: bool = True,
        batch_id: Optional[str] = None,
    ) -> ModelLoadResult:
        """Load ExtraFeature records from transformed data.
        
        Args:
            records: Generator of transformed records
            truncate: Whether to truncate the table before loading
            batch_id: Optional batch identifier
        
        Returns:
            ModelLoadResult with statistics
        """
        from data.models import ExtraFeature, PropertyRecord
        
        start_time = datetime.now()
        batch_id = batch_id or self._generate_batch_id()
        import_date = timezone.now()
        
        result = ModelLoadResult(
            model_name='ExtraFeature',
            batch_id=batch_id,
        )
        
        try:
            valid_accounts = self._get_valid_accounts()
            account_map = self._get_account_to_property_map()
            
            if truncate:
                self._truncate_table(ExtraFeature)
            
            buf: List[ExtraFeature] = []
            
            with transaction.atomic():
                for record in records:
                    acct = record.get('account_number', '').strip()
                    if not acct:
                        result.records_skipped += 1
                        continue
                    
                    if acct not in valid_accounts:
                        result.records_invalid += 1
                        continue
                    
                    # Get property ID
                    property_id = account_map.get(acct)
                    if not property_id:
                        result.records_invalid += 1
                        continue
                    
                    feature = ExtraFeature(
                        property_id=property_id,
                        account_number=acct,
                        feature_number=self._safe_int(record.get('feature_number')),
                        feature_code=str(record.get('feature_code', ''))[:10],
                        feature_description=str(record.get('feature_description', ''))[:255],
                        quantity=self._safe_decimal(record.get('quantity')),
                        area=self._safe_decimal(record.get('area')),
                        length=self._safe_decimal(record.get('length')),
                        width=self._safe_decimal(record.get('width')),
                        quality_code=str(record.get('quality_code', ''))[:10],
                        condition_code=str(record.get('condition_code', ''))[:10],
                        year_built=self._safe_int(record.get('year_built')),
                        value=self._safe_decimal(record.get('value')),
                        is_active=True,
                        import_date=import_date,
                        import_batch_id=batch_id,
                    )
                    
                    buf.append(feature)
                    
                    if len(buf) >= self.batch_size:
                        ExtraFeature.objects.bulk_create(buf, ignore_conflicts=True)
                        result.records_loaded += len(buf)
                        self.logger.info(
                            f"Loaded {result.records_loaded} extra feature records "
                            f"(invalid: {result.records_invalid}, skipped: {result.records_skipped})"
                        )
                        buf.clear()
                
                # Load remaining records
                if buf:
                    ExtraFeature.objects.bulk_create(buf, ignore_conflicts=True)
                    result.records_loaded += len(buf)
            
            self.logger.info(f"Completed: Loaded {result.records_loaded} extra feature records")
            
        except Exception as e:
            result.error = str(e)
            self.logger.exception(f"Error loading extra features: {e}")
        
        result.duration = (datetime.now() - start_time).total_seconds()
        return result
    
    def load_property_records(
        self,
        records: Generator[Dict[str, Any], None, None],
        truncate: bool = True,
        batch_id: Optional[str] = None,
    ) -> ModelLoadResult:
        """Load PropertyRecord records from transformed data.
        
        Args:
            records: Generator of transformed records
            truncate: Whether to truncate the table before loading
            batch_id: Optional batch identifier
        
        Returns:
            ModelLoadResult with statistics
        """
        from data.models import PropertyRecord
        
        start_time = datetime.now()
        batch_id = batch_id or self._generate_batch_id()
        
        result = ModelLoadResult(
            model_name='PropertyRecord',
            batch_id=batch_id,
        )
        
        try:
            if truncate:
                self._truncate_table(PropertyRecord)
                # Clear cached account data since we're truncating
                self.reset_cache()
            
            buf: List[PropertyRecord] = []
            
            with transaction.atomic():
                for record in records:
                    acct = record.get('account_number', '').strip()
                    if not acct:
                        result.records_skipped += 1
                        continue
                    
                    # Build address from components
                    street_num = str(record.get('street_number', '')).strip()
                    street_name_base = str(record.get('street_name', '')).strip()
                    street_suffix = str(record.get('street_suffix', '')).strip()
                    # Combine street name with suffix (e.g., "WALL" + "ST" -> "WALL ST")
                    street_name = f"{street_name_base} {street_suffix}".strip() if street_suffix else street_name_base
                    site_addr = str(record.get('site_addr_1', '')).strip()
                    address = site_addr or f"{street_num} {street_name}".strip()
                    
                    prop = PropertyRecord(
                        account_number=acct,
                        address=address[:255],
                        city=str(record.get('city', ''))[:100],
                        zipcode=str(record.get('zipcode', ''))[:20],
                        owner_name=str(record.get('owner_name', ''))[:255],
                        value=self._safe_decimal(record.get('value')),
                        assessed_value=self._safe_decimal(record.get('assessed_value')),
                        building_area=self._safe_decimal(record.get('building_area')),
                        land_area=self._safe_decimal(record.get('land_area')),
                        street_number=street_num[:16],
                        street_name=street_name[:128],
                    )
                    
                    buf.append(prop)
                    
                    if len(buf) >= self.batch_size:
                        PropertyRecord.objects.bulk_create(buf, ignore_conflicts=True)
                        result.records_loaded += len(buf)
                        self.logger.info(
                            f"Loaded {result.records_loaded} property records "
                            f"(skipped: {result.records_skipped})"
                        )
                        buf.clear()
                
                # Load remaining records
                if buf:
                    PropertyRecord.objects.bulk_create(buf, ignore_conflicts=True)
                    result.records_loaded += len(buf)
            
            self.logger.info(f"Completed: Loaded {result.records_loaded} property records")
            
            # Clear cached data since we've modified the table
            self.reset_cache()
            
        except Exception as e:
            result.error = str(e)
            self.logger.exception(f"Error loading property records: {e}")
            # Clear cache on error too, since table state is uncertain
            self.reset_cache()
        
        result.duration = (datetime.now() - start_time).total_seconds()
        return result
    
    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        """Safely convert a value to int."""
        if value is None:
            return None
        try:
            return int(float(str(value).strip()))
        except (ValueError, TypeError):
            return None
    
    @staticmethod
    def _safe_decimal(value: Any) -> Optional[float]:
        """Safely convert a value to float/decimal."""
        if value is None:
            return None
        try:
            return float(str(value).strip())
        except (ValueError, TypeError):
            return None
    
    def _get_bedrooms(self, account_num: str, building_num: int, record: Dict[str, Any]) -> Optional[int]:
        """
        Get bedroom count from fixtures aggregator or fallback to record.
        
        Args:
            account_num: Property account number
            building_num: Building number
            record: Transformed building record
            
        Returns:
            Bedroom count or None
        """
        # Try fixtures first
        bedroom_count = self.fixtures_aggregator.get_bedroom_count(account_num, building_num)
        if bedroom_count > 0:
            return bedroom_count
        
        # Fallback to record (building_res.txt columns if they exist)
        return self._safe_int(record.get('bedrooms'))
    
    def _get_bathrooms(self, account_num: str, building_num: int, record: Dict[str, Any]) -> Optional[float]:
        """
        Get total bathroom count from fixtures aggregator or fallback to record.
        
        Total bathrooms = full_baths + (half_baths * 0.5)
        
        Args:
            account_num: Property account number
            building_num: Building number
            record: Transformed building record
            
        Returns:
            Total bathroom count or None
        """
        # Try fixtures first
        bathroom_count = self.fixtures_aggregator.get_bathroom_count(account_num, building_num)
        if bathroom_count > 0:
            return bathroom_count
        
        # Fallback to record
        full_baths = self._safe_decimal(record.get('full_baths')) or 0
        half_baths = self._safe_int(record.get('half_baths')) or 0
        
        total = full_baths + (half_baths * 0.5)
        return total if total > 0 else None
    
    def _get_half_baths(self, account_num: str, building_num: int, record: Dict[str, Any]) -> Optional[int]:
        """
        Get half bathroom count from fixtures aggregator or fallback to record.
        
        Args:
            account_num: Property account number
            building_num: Building number
            record: Transformed building record
            
        Returns:
            Half bathroom count or None
        """
        # Try fixtures first
        fixtures = self.fixtures_aggregator.get_fixtures(account_num, building_num)
        half_bath_count = int(fixtures['half_baths'])
        if half_bath_count > 0:
            return half_bath_count
        
        # Fallback to record
        return self._safe_int(record.get('half_baths'))
