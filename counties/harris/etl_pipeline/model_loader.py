"""
ETL Pipeline Model Loader

Provides the bridge between the ETL pipeline and Django models.  Consumes the
same ``RowResult`` generators as the COPY path (``fast_loader``) and loads
them into PropertyRecord, BuildingDetail, and ExtraFeature via ``bulk_create``.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from django.db import connection, transaction
from django.db.models import Model
from django.utils import timezone

from .config import ETLConfig
from .fixtures_aggregator import FixturesAggregator
from .logging import ETLLogger
from .row_reader import BuildingRow, ExtraFeatureRow, PropertyRow, RowResult

logger = logging.getLogger(__name__)


@dataclass
class ModelLoadResult:
    """Result of loading records into a Django model.

    ``records_loaded`` is rows that actually reached the table, measured as a
    row-count delta rather than as "rows handed to bulk_create". Those are not
    the same number: every loader passes ``ignore_conflicts=True``, so a row
    whose key already exists is dropped silently. Counting what was offered
    made ``records_loaded`` read like a completeness signal while overstating
    it — on a real HCAD run, 905,338 offered against 871,769 persisted.

    ``records_conflicted`` is that gap, which is worth surfacing rather than
    hiding: it counts duplicate keys within the source file.
    """

    model_name: str
    records_loaded: int = 0
    records_conflicted: int = 0
    records_invalid: int = 0
    records_skipped: int = 0
    batch_id: str = ""
    duration: float = 0.0
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None

    @property
    def total_processed(self) -> int:
        return self.records_loaded + self.records_invalid + self.records_skipped


class ModelLoader:
    """Loads typed rows into Django models.

    Handles:
    - PropertyRecord loading from real_acct.txt
    - BuildingDetail loading from building_res.txt
    - ExtraFeature loading from extra_features.txt
    """

    def __init__(
        self,
        config: ETLConfig,
        etl_logger: ETLLogger | None = None,
        batch_size: int = 5000,
    ):
        self.config = config
        self.logger = etl_logger or ETLLogger(name="model_loader")
        self.batch_size = batch_size
        self._valid_accounts: set[str] | None = None
        self._account_to_property: dict[str, int] | None = None
        self.fixtures_aggregator = FixturesAggregator()

    def reset_cache(self) -> None:
        """Clear cached account lookups. Call after loading PropertyRecords."""
        self._valid_accounts = None
        self._account_to_property = None

    def _generate_batch_id(self) -> str:
        """Generate a unique batch ID for tracking imports."""
        return timezone.now().strftime("%Y%m%d_%H%M%S")

    def _get_valid_accounts(self) -> set[str]:
        """Get set of valid account numbers from PropertyRecord."""
        if self._valid_accounts is None:
            from counties.harris.models import PropertyRecord

            self._valid_accounts = set(
                PropertyRecord.objects.filter(is_residential=True).values_list(
                    "account_number", flat=True
                )
            )
            self.logger.info(f"Loaded {len(self._valid_accounts)} valid account numbers")
        return self._valid_accounts

    def _get_account_to_property_map(self) -> dict[str, int]:
        """Get mapping of account numbers to PropertyRecord IDs."""
        if self._account_to_property is None:
            from counties.harris.models import PropertyRecord

            self._account_to_property = dict(
                PropertyRecord.objects.filter(is_residential=True).values_list(
                    "account_number", "id"
                )
            )
            self.logger.info(f"Loaded {len(self._account_to_property)} account->property mappings")
        return self._account_to_property

    def _truncate_table(self, model_class: type[Model]) -> None:
        """Truncate a table for clean import."""
        table_name = model_class._meta.db_table
        self.logger.info(f"Truncating table {table_name}...")
        with connection.cursor() as cursor:
            cursor.execute(f'TRUNCATE TABLE "{table_name}" RESTART IDENTITY CASCADE')
        self.logger.info(f"Table {table_name} truncated successfully")

    def load_building_details(
        self,
        rows: Iterator[RowResult],
        truncate: bool = True,
        batch_id: str | None = None,
    ) -> ModelLoadResult:
        """Load BuildingRow rows into BuildingDetail."""
        from counties.harris.models import BuildingDetail

        start_time = datetime.now()
        batch_id = batch_id or self._generate_batch_id()
        import_date = timezone.now()

        result = ModelLoadResult(
            model_name="BuildingDetail",
            batch_id=batch_id,
        )

        try:
            buf: list[BuildingDetail] = []
            offered = 0

            # Truncate and reload run in ONE transaction so a mid-load failure
            # rolls the truncate back instead of leaving the table empty.
            with transaction.atomic():
                if truncate:
                    self._truncate_table(BuildingDetail)

                # Snapshot inside the transaction, after any truncate: what
                # lands is measured as a delta, because bulk_create's
                # ignore_conflicts silently drops duplicate keys.
                rows_before = BuildingDetail.objects.count()

                for item in rows:
                    if item.skip:
                        result.records_skipped += 1
                        continue
                    if item.invalid:
                        result.records_invalid += 1
                        continue
                    if not isinstance(item.row, BuildingRow):
                        continue

                    row = item.row
                    building = BuildingDetail(
                        property_id=row.property_id,
                        account_number=row.account_number,
                        building_number=row.building_number,
                        building_type=row.building_type,
                        building_style=row.building_style,
                        building_class=row.building_class,
                        quality_code=row.quality_code,
                        condition_code=row.condition_code,
                        year_built=row.year_built,
                        year_remodeled=row.year_remodeled,
                        effective_year=row.effective_year,
                        heat_area=row.heat_area,
                        base_area=row.base_area,
                        gross_area=row.gross_area,
                        stories=row.stories,
                        foundation_type=row.foundation_type,
                        exterior_wall=row.exterior_wall,
                        roof_cover=row.roof_cover,
                        roof_type=row.roof_type,
                        bedrooms=row.bedrooms,
                        bathrooms=row.bathrooms,
                        half_baths=row.half_baths,
                        fireplaces=row.fireplaces,
                        is_active=row.is_active,
                        import_date=import_date,
                        import_batch_id=batch_id,
                    )

                    buf.append(building)

                    if len(buf) >= self.batch_size:
                        BuildingDetail.objects.bulk_create(buf, ignore_conflicts=True)
                        offered += len(buf)
                        self.logger.info(
                            f"Offered {offered} building records "
                            f"(invalid: {result.records_invalid}, skipped: {result.records_skipped})"
                        )
                        buf.clear()

                # Load remaining records
                if buf:
                    BuildingDetail.objects.bulk_create(buf, ignore_conflicts=True)
                    offered += len(buf)

                result.records_loaded = BuildingDetail.objects.count() - rows_before
                result.records_conflicted = offered - result.records_loaded

            self.logger.info(
                f"Completed: Loaded {result.records_loaded} building detail records"
                f" ({result.records_conflicted} dropped as duplicate keys)"
            )

        except Exception as e:
            result.error = str(e)
            self.logger.exception(f"Error loading building details: {e}")

        result.duration = (datetime.now() - start_time).total_seconds()
        return result

    def load_extra_features(
        self,
        rows: Iterator[RowResult],
        truncate: bool = True,
        batch_id: str | None = None,
    ) -> ModelLoadResult:
        """Load ExtraFeatureRow rows into ExtraFeature."""
        from counties.harris.models import ExtraFeature

        start_time = datetime.now()
        batch_id = batch_id or self._generate_batch_id()
        import_date = timezone.now()

        result = ModelLoadResult(
            model_name="ExtraFeature",
            batch_id=batch_id,
        )

        try:
            buf: list[ExtraFeature] = []
            offered = 0

            # Truncate and reload run in ONE transaction so a mid-load failure
            # rolls the truncate back instead of leaving the table empty.
            with transaction.atomic():
                if truncate:
                    self._truncate_table(ExtraFeature)

                # Snapshot inside the transaction, after any truncate: what
                # lands is measured as a delta, because bulk_create's
                # ignore_conflicts silently drops duplicate keys.
                rows_before = ExtraFeature.objects.count()

                for item in rows:
                    if item.skip:
                        result.records_skipped += 1
                        continue
                    if item.invalid:
                        result.records_invalid += 1
                        continue
                    if not isinstance(item.row, ExtraFeatureRow):
                        continue

                    row = item.row
                    feature = ExtraFeature(
                        property_id=row.property_id,
                        account_number=row.account_number,
                        feature_number=row.feature_number,
                        feature_code=row.feature_code,
                        feature_description=row.feature_description,
                        quantity=row.quantity,
                        length=row.length,
                        width=row.width,
                        quality_code=row.quality_code,
                        condition_code=row.condition_code,
                        year_built=row.year_built,
                        value=row.value,
                        is_active=row.is_active,
                        import_date=import_date,
                        import_batch_id=batch_id,
                    )

                    buf.append(feature)

                    if len(buf) >= self.batch_size:
                        ExtraFeature.objects.bulk_create(buf, ignore_conflicts=True)
                        offered += len(buf)
                        self.logger.info(
                            f"Offered {offered} extra feature records "
                            f"(invalid: {result.records_invalid}, skipped: {result.records_skipped})"
                        )
                        buf.clear()

                # Load remaining records
                if buf:
                    ExtraFeature.objects.bulk_create(buf, ignore_conflicts=True)
                    offered += len(buf)

                result.records_loaded = ExtraFeature.objects.count() - rows_before
                result.records_conflicted = offered - result.records_loaded

            self.logger.info(
                f"Completed: Loaded {result.records_loaded} extra feature records"
                f" ({result.records_conflicted} dropped as duplicate keys)"
            )

        except Exception as e:
            result.error = str(e)
            self.logger.exception(f"Error loading extra features: {e}")

        result.duration = (datetime.now() - start_time).total_seconds()
        return result

    def load_property_records(
        self,
        rows: Iterator[RowResult],
        truncate: bool = True,
        batch_id: str | None = None,
    ) -> ModelLoadResult:
        """Load PropertyRow rows into PropertyRecord."""
        from counties.harris.models import PropertyRecord

        start_time = datetime.now()
        batch_id = batch_id or self._generate_batch_id()

        result = ModelLoadResult(
            model_name="PropertyRecord",
            batch_id=batch_id,
        )

        try:
            buf: list[PropertyRecord] = []
            offered = 0

            # Truncate and reload run in ONE transaction so a mid-load failure
            # rolls the truncate back instead of leaving the table empty.
            with transaction.atomic():
                if truncate:
                    self._truncate_table(PropertyRecord)
                    # Clear cached account data since we're truncating
                    self.reset_cache()

                # Snapshot inside the transaction, after any truncate: what
                # lands is measured as a delta, because bulk_create's
                # ignore_conflicts silently drops duplicate keys.
                rows_before = PropertyRecord.objects.count()

                for item in rows:
                    if item.skip:
                        result.records_skipped += 1
                        continue
                    if not isinstance(item.row, PropertyRow):
                        continue

                    row = item.row
                    prop = PropertyRecord(
                        account_number=row.account_number,
                        address=row.address,
                        city=row.city,
                        zipcode=row.zipcode,
                        owner_name=row.owner_name,
                        value=row.value,
                        assessed_value=row.assessed_value,
                        building_area=row.building_area,
                        land_area=row.land_area,
                        state_class=row.state_class,
                        is_residential=row.is_residential,
                        is_data_ready=row.is_data_ready,
                        street_number=row.street_number,
                        street_name=row.street_name,
                    )

                    buf.append(prop)

                    if len(buf) >= self.batch_size:
                        PropertyRecord.objects.bulk_create(buf, ignore_conflicts=True)
                        offered += len(buf)
                        self.logger.info(
                            f"Offered {offered} property records "
                            f"(skipped: {result.records_skipped})"
                        )
                        buf.clear()

                # Load remaining records
                if buf:
                    PropertyRecord.objects.bulk_create(buf, ignore_conflicts=True)
                    offered += len(buf)

                result.records_loaded = PropertyRecord.objects.count() - rows_before
                result.records_conflicted = offered - result.records_loaded

            self.logger.info(
                f"Completed: Loaded {result.records_loaded} property records"
                f" ({result.records_conflicted} dropped as duplicate keys)"
            )

            # Clear cached data since we've modified the table
            self.reset_cache()

        except Exception as e:
            result.error = str(e)
            self.logger.exception(f"Error loading property records: {e}")
            # Clear cache on error too, since table state is uncertain
            self.reset_cache()

        result.duration = (datetime.now() - start_time).total_seconds()
        return result
