"""Legacy orphan-cleanup and account-mapping helpers.

The modern ETL pipeline uses ``TRUNCATE ... RESTART IDENTITY CASCADE``
(see ``etl_pipeline/model_loader.py``), so loading never produces
orphaned BuildingDetail/ExtraFeature rows. These helpers survive only
for ``reconcile_property_data`` -- a one-shot cleanup tool for
databases that ran the legacy soft-delete import path and may still
carry orphaned rows from that era.
"""

from __future__ import annotations

import logging

from django.db import transaction

from counties.harris.models import PropertyRecord

logger = logging.getLogger(__name__)


def load_account_property_map(
    *,
    account_numbers: set[str] | None = None,
    residential_only: bool = True,
) -> dict[str, int]:
    """Load account_number -> PropertyRecord.id mapping.

    If account_numbers is provided, limit the mapping query to that account set.
    """
    query = PropertyRecord.objects.all()
    if residential_only:
        query = query.filter(is_residential=True)
    if account_numbers is not None:
        query = query.filter(account_number__in=account_numbers)
    return dict(query.values_list("account_number", "id"))


def link_orphaned_records(chunk_size: int = 5000) -> dict:
    """Link orphaned BuildingDetail and ExtraFeature records to their PropertyRecord.

    This handles cases where features were imported before the property was created,
    or where the property link failed during initial import.

    Returns:
        Dictionary with counts of linked records and validation stats
    """
    from counties.harris.models import BuildingDetail, ExtraFeature

    results = {
        "buildings_linked": 0,
        "features_linked": 0,
        "buildings_invalid": 0,
        "features_invalid": 0,
    }
    account_to_property = load_account_property_map()

    logger.info("Linking orphaned building details...")

    # Find buildings without property links
    orphaned_buildings = BuildingDetail.objects.filter(property__isnull=True)
    total_orphaned = orphaned_buildings.count()
    logger.info("Found %s orphaned building records", total_orphaned)

    batch = []
    with transaction.atomic():
        for building in orphaned_buildings.iterator(chunk_size=chunk_size):
            if building.account_number:
                property_id = account_to_property.get(building.account_number)
                if property_id:
                    building.property_id = property_id
                    batch.append(building)

                    if len(batch) >= chunk_size:
                        BuildingDetail.objects.bulk_update(batch, ["property"])
                        results["buildings_linked"] += len(batch)
                        logger.info(
                            "Linked %s building records...",
                            results["buildings_linked"],
                        )
                        batch.clear()
                else:
                    results["buildings_invalid"] += 1

        # Update remaining batch
        if batch:
            BuildingDetail.objects.bulk_update(batch, ["property"])
            results["buildings_linked"] += len(batch)

    logger.info(
        "Completed building linking: %s linked, %s invalid",
        results["buildings_linked"],
        results["buildings_invalid"],
    )

    # Now link orphaned features
    logger.info("Linking orphaned extra features...")

    orphaned_features = ExtraFeature.objects.filter(property__isnull=True)
    total_orphaned = orphaned_features.count()
    logger.info("Found %s orphaned feature records", total_orphaned)

    batch = []
    with transaction.atomic():
        for feature in orphaned_features.iterator(chunk_size=chunk_size):
            if feature.account_number:
                property_id = account_to_property.get(feature.account_number)
                if property_id:
                    feature.property_id = property_id
                    batch.append(feature)

                    if len(batch) >= chunk_size:
                        ExtraFeature.objects.bulk_update(batch, ["property"])
                        results["features_linked"] += len(batch)
                        logger.info("Linked %s feature records...", results["features_linked"])
                        batch.clear()
                else:
                    results["features_invalid"] += 1

        # Update remaining batch
        if batch:
            ExtraFeature.objects.bulk_update(batch, ["property"])
            results["features_linked"] += len(batch)

    logger.info(
        "Completed feature linking: %s linked, %s invalid",
        results["features_linked"],
        results["features_invalid"],
    )
    logger.info("Total results: %s", results)

    return results
