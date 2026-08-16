"""Recompute PropertyRecord.is_data_ready after a pipeline load stage.

The single home for this function. It moved out of ``counties/harris/etl.py``
so the pipeline no longer reaches across a package boundary for what is
logically a post-load step; ``etl.py``'s legacy loaders and the
``reconcile_property_data`` command import it from here. The orchestrator
calls it once after a successful load (see
``ETLOrchestrator._refresh_readiness_once``).
"""

from __future__ import annotations

import logging

from django.db import connection
from django.db.models import Exists, OuterRef

from counties.harris.models import BuildingDetail, PropertyRecord

logger = logging.getLogger(__name__)


# Clear-then-set, but each pass writes only the rows that actually change.
#
# Why this matters: is_data_ready is indexed, so updating it can never be a HOT
# update, and a non-HOT update inserts new entries into *all 13 indexes* on
# data_propertyrecord (~730MB of them). The unguarded form cleared every ready
# row and set them all straight back, so a re-run rewrote ~1.17M rows twice to
# reach the state it was already in — measured at 162s + 376s = 538s, against
# 4.7s once guarded. reconcile_property_data calls this twice per --apply.
#
# The set pass below is the original statement plus `AND is_data_ready = false`.
# That extra predicate is purely restrictive, so a cold run straight after the
# property COPY (where every row is already false) matches exactly the same rows
# and plans the same way as before — this cannot regress the full-ETL path.
_CLEAR_NO_LONGER_READY = """
UPDATE data_propertyrecord p
SET is_data_ready = false
WHERE p.is_data_ready = true
  AND NOT (
      p.is_residential
      AND EXISTS (
          SELECT 1 FROM data_buildingdetail b
          WHERE b.property_id = p.id
            AND b.is_active = true
            AND b.bedrooms IS NOT NULL
            AND b.bathrooms IS NOT NULL
      )
      AND EXISTS (
          SELECT 1 FROM data_parcelgeometry g
          WHERE g.account_number = p.account_number
            AND g.county = 'harris'
            AND g.latitude IS NOT NULL
            AND g.longitude IS NOT NULL
      )
  )
"""

_SET_NEWLY_READY = """
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
  AND data_propertyrecord.is_data_ready = false
  AND data_propertyrecord.account_number IN (
      SELECT account_number FROM data_parcelgeometry
      WHERE county = 'harris'
        AND latitude IS NOT NULL
        AND longitude IS NOT NULL
  )
"""


def refresh_property_readiness() -> dict:
    """Recompute PropertyRecord.is_data_ready from building, room, and GIS completeness.

    Writes only the rows whose readiness actually changes. ``ready_properties_set``
    stays the *total* number of ready properties (what callers report to users);
    ``ready_properties_changed`` / ``ready_properties_cleared`` are the deltas.
    """
    ready_buildings = BuildingDetail.objects.filter(
        property_id=OuterRef("pk"),
        is_active=True,
        bedrooms__isnull=False,
        bathrooms__isnull=False,
    )

    residential_properties = PropertyRecord.objects.filter(is_residential=True)
    results = {
        "properties_evaluated": PropertyRecord.objects.count(),
        "residential_properties": residential_properties.count(),
    }

    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(_CLEAR_NO_LONGER_READY)
            results["ready_properties_cleared"] = cursor.rowcount
            cursor.execute(_SET_NEWLY_READY)
            results["ready_properties_changed"] = cursor.rowcount
    else:
        from counties.common.tax_models import ParcelGeometry

        accounts_with_coords = ParcelGeometry.objects.filter(
            county="harris", latitude__isnull=False, longitude__isnull=False
        ).values_list("account_number", flat=True)

        should_be_ready = (
            PropertyRecord.objects.filter(
                is_residential=True,
                account_number__in=accounts_with_coords,
            )
            .annotate(has_ready_building=Exists(ready_buildings))
            .filter(has_ready_building=True)
        )
        ready_ids = list(should_be_ready.values_list("pk", flat=True))

        results["ready_properties_changed"] = (
            PropertyRecord.objects.filter(pk__in=ready_ids)
            .exclude(is_data_ready=True)
            .update(is_data_ready=True)
        )
        results["ready_properties_cleared"] = (
            PropertyRecord.objects.filter(is_data_ready=True)
            .exclude(pk__in=ready_ids)
            .update(is_data_ready=False)
        )

    results["ready_properties_set"] = PropertyRecord.objects.filter(is_data_ready=True).count()

    logger.info(
        "Refreshed property readiness: %s/%s residential properties ready "
        "(%s newly ready, %s cleared)",
        results["ready_properties_set"],
        results["residential_properties"],
        results["ready_properties_changed"],
        results["ready_properties_cleared"],
    )
    return results
