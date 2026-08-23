"""
ETL Pipeline Model Loader

Provides ORM persistence adapters for Harris translated rows.

Source parsing and business rules live in ``row_reader``. This module creates
Django model instances from its database-neutral ``RowResult`` values.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from django.db import connection, transaction
from django.db.models import Model
from django.utils import timezone

from .config import ETLConfig
from .fixtures_aggregator import FixturesAggregator
from .logging import ETLLogger
from .row_reader import RowResult, RowValue

logger = logging.getLogger(__name__)

ModelBuilder = Callable[[dict[str, RowValue], datetime, str], Model]


@dataclass
class ModelLoadResult:
    """Result of loading records into a Django model."""

    model_name: str
    records_loaded: int = 0
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
    """Loads translated rows into Django models.

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
        self._account_to_property: dict[str, int] | None = None
        self.fixtures_aggregator = FixturesAggregator()

    def reset_cache(self) -> None:
        """Clear cached account lookups. Call after loading PropertyRecords."""
        self._account_to_property = None

    def _generate_batch_id(self) -> str:
        """Generate a unique batch ID for tracking imports."""
        return timezone.now().strftime("%Y%m%d_%H%M%S")

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

    @staticmethod
    def _row_key(row: RowResult, key_fields: tuple[str, ...]) -> tuple[RowValue, ...] | None:
        """Return a database uniqueness key when every key part is present."""
        record = row.as_dict()
        key = tuple(record[field] for field in key_fields)
        return None if any(value is None for value in key) else key

    @staticmethod
    def _existing_keys(
        model_class: type[Model],
        key_fields: tuple[str, ...],
        candidate_keys: set[tuple[RowValue, ...]],
    ) -> set[tuple[RowValue, ...]]:
        """Read existing unique keys relevant to one bounded insert batch."""
        if not candidate_keys:
            return set()

        if len(key_fields) == 1:
            values = {key[0] for key in candidate_keys}
            return {
                (value,)
                for value in model_class.objects.filter(**{f"{key_fields[0]}__in": values}).values_list(
                    key_fields[0], flat=True
                )
            }

        account_numbers = {key[0] for key in candidate_keys}
        return set(
            model_class.objects.filter(account_number__in=account_numbers).values_list(*key_fields)
        )

    def _load_rows(
        self,
        rows: Iterable[RowResult],
        *,
        model_class: type[Model],
        model_name: str,
        key_fields: tuple[str, ...],
        build_model: ModelBuilder,
        truncate: bool,
        batch_id: str | None,
    ) -> ModelLoadResult:
        """Persist translated rows with consistent skip and conflict accounting."""
        started_at = datetime.now()
        resolved_batch_id = batch_id or self._generate_batch_id()
        import_date = timezone.now()
        result = ModelLoadResult(model_name=model_name, batch_id=resolved_batch_id)
        seen_keys: set[tuple[RowValue, ...]] = set()
        pending: list[tuple[RowResult, tuple[RowValue, ...] | None]] = []

        def flush() -> None:
            if not pending:
                return

            keys = {key for _, key in pending if key is not None}
            existing = self._existing_keys(model_class, key_fields, keys)
            models: list[Model] = []
            for row, key in pending:
                if key is not None and key in existing:
                    result.records_skipped += 1
                    continue
                models.append(build_model(row.as_dict(), import_date, resolved_batch_id))

            if models:
                # The preflight above gives deterministic counts in the ETL's
                # single-writer transaction; ignore_conflicts still protects a
                # concurrent writer without overwriting data.
                model_class.objects.bulk_create(models, ignore_conflicts=True)
                result.records_loaded += len(models)
            pending.clear()

        try:
            with transaction.atomic():
                if truncate:
                    self._truncate_table(model_class)

                for row in rows:
                    if row.skip:
                        result.records_skipped += 1
                        continue
                    if row.invalid:
                        result.records_invalid += 1
                        continue

                    key = self._row_key(row, key_fields)
                    if key is not None:
                        if key in seen_keys:
                            result.records_skipped += 1
                            continue
                        seen_keys.add(key)
                    pending.append((row, key))
                    if len(pending) >= self.batch_size:
                        flush()
                flush()
        except Exception as exc:
            result.error = str(exc)
            # The enclosing transaction rolls every flushed batch back, so do
            # not report attempted rows as persisted after a failed import.
            result.records_loaded = 0
            self.logger.exception("Error loading %s rows: %s", model_name, exc)

        result.duration = (datetime.now() - started_at).total_seconds()
        return result

    def load_building_details(
        self,
        rows: Iterable[RowResult],
        truncate: bool = True,
        batch_id: str | None = None,
    ) -> ModelLoadResult:
        """Persist translated BuildingDetail rows."""
        from counties.harris.models import BuildingDetail

        return self._load_rows(
            rows,
            model_class=BuildingDetail,
            model_name="BuildingDetail",
            key_fields=("account_number", "building_number"),
            build_model=lambda record, import_date, resolved_batch_id: BuildingDetail(
                **record,
                import_date=import_date,
                import_batch_id=resolved_batch_id,
            ),
            truncate=truncate,
            batch_id=batch_id,
        )

    def load_extra_features(
        self,
        rows: Iterable[RowResult],
        truncate: bool = True,
        batch_id: str | None = None,
    ) -> ModelLoadResult:
        """Persist translated ExtraFeature rows."""
        from counties.harris.models import ExtraFeature

        return self._load_rows(
            rows,
            model_class=ExtraFeature,
            model_name="ExtraFeature",
            key_fields=("account_number", "feature_code", "feature_number"),
            build_model=lambda record, import_date, resolved_batch_id: ExtraFeature(
                **record,
                import_date=import_date,
                import_batch_id=resolved_batch_id,
            ),
            truncate=truncate,
            batch_id=batch_id,
        )

    def load_property_records(
        self,
        rows: Iterable[RowResult],
        truncate: bool = True,
        batch_id: str | None = None,
    ) -> ModelLoadResult:
        """Persist translated PropertyRecord rows."""
        from counties.harris.models import PropertyRecord

        result = self._load_rows(
            rows,
            model_class=PropertyRecord,
            model_name="PropertyRecord",
            key_fields=("account_number",),
            build_model=lambda record, _import_date, _resolved_batch_id: PropertyRecord(**record),
            truncate=truncate,
            batch_id=batch_id,
        )
        self.reset_cache()
        return result
