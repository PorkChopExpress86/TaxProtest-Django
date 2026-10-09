"""Recompute PropertyRecord.is_data_ready after a pipeline load stage.

Moved here from ``counties/harris/etl.py`` so the ETL pipeline no longer
reaches across a package boundary for a function that is logically a
post-load pipeline step. The orchestrator calls this once after a
successful load (see the private execution behind ``run_harris_import``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import connection
from django.db.models import Exists, OuterRef

from counties.harris.models import BuildingDetail, PropertyRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReadinessSummary:
    """What a readiness refresh evaluated and set.

    A dataclass rather than a dict because these field names are a contract:
    ``reconcile_property_data`` prints ``ready_properties_set`` and records the
    whole summary as operation evidence.
    """

    properties_evaluated: int
    residential_properties: int
    ready_properties_cleared: int
    ready_properties_set: int


def refresh_property_readiness() -> ReadinessSummary:
    """Recompute PropertyRecord.is_data_ready based on building, room, and GIS completeness."""
    ready_buildings = BuildingDetail.objects.filter(
        property_id=OuterRef("pk"),
        is_active=True,
        bedrooms__isnull=False,
        bathrooms__isnull=False,
    )

    residential_properties = PropertyRecord.objects.filter(is_residential=True)
    properties_evaluated = PropertyRecord.objects.count()
    residential_count = residential_properties.count()

    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE data_propertyrecord
                SET is_data_ready = false
                WHERE is_data_ready = true;
            """)
            cleared = cursor.rowcount

            cursor.execute("""
                UPDATE data_propertyrecord
                SET is_data_ready = true
                FROM (
                    SELECT DISTINCT property_id
                    FROM data_buildingdetail
                    WHERE is_active = true
                      AND bedrooms IS NOT NULL
                      AND bathrooms IS NOT NULL
                ) b
                WHERE data_propertyrecord.id = b.property_id
                  AND data_propertyrecord.is_residential = true
                  AND data_propertyrecord.latitude IS NOT NULL
                  AND data_propertyrecord.longitude IS NOT NULL;
            """)
            ready = cursor.rowcount
    else:
        cleared = PropertyRecord.objects.filter(is_data_ready=True).update(is_data_ready=False)

        ready = (
            residential_properties.filter(
                latitude__isnull=False,
                longitude__isnull=False,
            )
            .annotate(has_ready_building=Exists(ready_buildings))
            .filter(has_ready_building=True)
            .update(is_data_ready=True)
        )

    summary = ReadinessSummary(
        properties_evaluated=properties_evaluated,
        residential_properties=residential_count,
        ready_properties_cleared=cleared,
        ready_properties_set=ready,
    )
    logger.info(
        "Refreshed property readiness: %s/%s residential properties ready",
        summary.ready_properties_set,
        summary.residential_properties,
    )
    return summary
