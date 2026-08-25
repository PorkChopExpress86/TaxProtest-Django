"""Reconcile legacy rows through the authoritative Harris ETL package."""

from __future__ import annotations

import logging

from django.db import transaction

from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord

logger = logging.getLogger(__name__)


def link_orphaned_records(chunk_size: int = 5000) -> dict[str, int]:
    """Link orphaned buildings and features to their residential properties."""
    results = {
        "buildings_linked": 0,
        "features_linked": 0,
        "buildings_invalid": 0,
        "features_invalid": 0,
    }
    account_to_property = dict(
        PropertyRecord.objects.filter(is_residential=True).values_list("account_number", "id")
    )

    building_results = _link_orphans(
        queryset=BuildingDetail.objects.filter(property__isnull=True),
        account_to_property=account_to_property,
        chunk_size=chunk_size,
        result_key="buildings_linked",
        invalid_key="buildings_invalid",
        model_name="building",
    )
    results.update(building_results)

    feature_results = _link_orphans(
        queryset=ExtraFeature.objects.filter(property__isnull=True),
        account_to_property=account_to_property,
        chunk_size=chunk_size,
        result_key="features_linked",
        invalid_key="features_invalid",
        model_name="extra feature",
    )
    results.update(feature_results)
    logger.info("Linked orphaned Harris rows: %s", results)
    return results


def _link_orphans(
    *,
    queryset,
    account_to_property: dict[str, int],
    chunk_size: int,
    result_key: str,
    invalid_key: str,
    model_name: str,
) -> dict[str, int]:
    """Link one model's orphaned rows while keeping updates transactional."""
    results = {result_key: 0, invalid_key: 0}
    pending = []
    logger.info("Linking orphaned %s rows", model_name)

    with transaction.atomic():
        for record in queryset.iterator(chunk_size=chunk_size):
            if not record.account_number:
                continue
            property_id = account_to_property.get(record.account_number)
            if property_id is None:
                results[invalid_key] += 1
                continue
            record.property_id = property_id
            pending.append(record)
            if len(pending) >= chunk_size:
                queryset.model.objects.bulk_update(pending, ["property"])
                results[result_key] += len(pending)
                pending.clear()

        if pending:
            queryset.model.objects.bulk_update(pending, ["property"])
            results[result_key] += len(pending)

    return results
