"""Brazos Central Appraisal District (BCAD) PACS-format models.

Maps to the real BCAD certified-data files (verified against a real 2025
export; see ``counties/brazos/parsers/pacs.py`` for the fixed-width field layout):
  - APPRAISAL_INFO.TXT               -> PropertyAccount (mailing_* fields only)
  - APPRAISAL_LAND_DETAIL.TXT        -> PropertyLand
  - APPRAISAL_IMPROVEMENT_INFO.TXT   -> PropertyImprovement
  - APPRAISAL_IMPROVEMENT_DETAIL.TXT -> PropertyImprovementDetail
  - APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT -> PropertyBuildingCharacteristic
    (bedrooms/bathrooms/room count/material codes, aggregated wide) and
    PropertyExtraFeature (fireplaces/patios/carports/pools/etc, one row per
    feature) — see both models' docstrings for why the split
  - APPRAISAL_ENTITY.TXT             -> PropertyEntity (id/code lookup only,
    NOT currently ingested — this file has no entity_name/type/rate columns,
    see PropertyEntity's docstring)
  - APPRAISAL_ENTITY_INFO.TXT        -> counties.harris.models.PropertyJurisdictionExemption
    (NOT a brazos_cad model — shared with Harris, see wayfinder ticket #9);
    also rolls up PropertyAccount.assessed_value (see below)

PropertyAccount.state_class/latitude/longitude/living_area/year_built/
class_code/situs_* are NOT sourced from APPRAISAL_INFO.TXT (values/
state_class/coordinates/building basics were never located in that file's
decoded fields — see docs/research/brazos-values.md, wayfinder ticket #5).
They're populated separately by ``load_brazos_gis`` from BCAD's GIS parcel
shapefile (see docs/research/brazos-gis-parcel-shapefile.md, wayfinder
tickets #4-#7), which is also the genuine property-location (situs) source
— the address block actually present in APPRAISAL_INFO.TXT is the owner's
*mailing* address (see mailing_address's docstring below), confirmed by
cross-checking against the shapefile's separate mailing-address fields
(ticket #4).

PropertyAccount.assessed_value is NOT sourced from the GIS shapefile either
(despite carrying a plausible-looking "market" field) -- verified against
BCAD's own live property search that the shapefile is a rolling current/
preliminary snapshot, not a frozen certified-year archive, so its dollar
values don't actually match the target tax_year. assessed_value is instead
rolled up in ``load_brazos_cad``'s entity-info step from
APPRAISAL_ENTITY_INFO.TXT, which is genuinely tax_year-accurate.
total_value/land_value/improvement_value have no verified tax_year-accurate
Brazos source yet and are intentionally left unpopulated.

``is_residential`` is derived from ``state_class`` at GIS-ingest time only
once that mapping has been verified for Brazos's own code vocabulary — not
yet done, so it remains at its default (False) for now.
"""

from __future__ import annotations

from django.db import models
from django.db.models.functions import Now


class PropertyAccount(models.Model):
    """Maps to BCAD APPRAISAL_INFO.TXT — one row per (account, tax_year)."""

    prop_id = models.CharField(max_length=32, db_index=True)
    tax_year = models.PositiveIntegerField(db_index=True)

    # True physical (situs) location — sourced from the GIS parcel shapefile's
    # situs_num/situs_stre/situs_st_1/situs_st_2/situs_unit fields via
    # load_brazos_gis, NOT from APPRAISAL_INFO.TXT (that's mailing_* below).
    # Empty until load_brazos_gis has been run for this tax_year.
    situs_address = models.CharField(max_length=255, blank=True)
    situs_city = models.CharField(max_length=100, blank=True)
    situs_state = models.CharField(max_length=2, blank=True)
    situs_zip = models.CharField(max_length=10, blank=True)

    # The owner's mailing address, sourced from APPRAISAL_INFO.TXT's address
    # block via load_brazos_cad -- confirmed NOT the property's physical
    # location (see module docstring; cross-checked against the shapefile's
    # separate mailing-address fields, wayfinder ticket #4).
    mailing_address = models.CharField(max_length=255, blank=True)
    mailing_city = models.CharField(max_length=100, blank=True)
    mailing_state = models.CharField(max_length=2, blank=True)
    mailing_zip = models.CharField(max_length=10, blank=True)

    owner_name = models.CharField(max_length=255, blank=True, db_index=True)

    # total_value/land_value/improvement_value: no verified tax_year-accurate
    # Brazos source exists yet (the GIS shapefile's market/Land_Val/Imprv_Val
    # fields are current/preliminary, not certified-year -- see module
    # docstring), so these are intentionally left unpopulated.
    total_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    land_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    improvement_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    # Rolled up in load_brazos_cad's entity-info step from
    # APPRAISAL_ENTITY_INFO.TXT's assessed_val (genuinely tax_year-accurate)
    # -- NOT from the GIS shapefile. See module docstring.
    assessed_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    # state_class ("state_cd" in the shapefile) is sourced but is_residential
    # is deliberately NOT derived from it yet -- doing so needs the same kind
    # of code-vocabulary verification data/residential.py did for HCAD's own
    # codes, not yet done for Brazos's. See module docstring.
    state_class = models.CharField(max_length=10, blank=True, db_index=True)
    is_residential = models.BooleanField(default=False, db_index=True, db_default=False)

    # Coordinates (reprojected from the shapefile's native EPSG:2277 to
    # WGS84) and basic building characteristics, also GIS-sourced -- no
    # bedroom/bathroom equivalent exists in the shapefile (see
    # docs/research/brazos-building-characteristics.md).
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    living_area = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    year_built = models.IntegerField(null=True, blank=True)
    class_code = models.CharField(max_length=16, blank=True)

    # Source/audit metadata, populated by the loader alongside the parsed fields.
    source_file = models.CharField(max_length=255, blank=True, db_default="")
    ingested_at = models.DateTimeField(auto_now_add=True, db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prop_id", "tax_year"], name="brazoscad_account_unique"
            ),
        ]
        indexes = [
            models.Index(fields=["tax_year", "prop_id"]),
        ]
        verbose_name = "BCAD Property Account"
        verbose_name_plural = "BCAD Property Accounts"

    def __str__(self) -> str:
        return f"PropertyAccount(prop_id={self.prop_id}, year={self.tax_year})"


class PropertyLand(models.Model):
    """Maps to BCAD APPRAISAL_LAND_DETAIL.TXT — land parcels per account."""

    prop_id = models.CharField(max_length=32, db_index=True)
    tax_year = models.PositiveIntegerField(db_index=True)

    # land_seq is a global sequence across the whole file, not per-property —
    # uniqueness is only guaranteed by (prop_id, tax_year, land_seq) together.
    land_seq = models.IntegerField(default=0, db_index=True)
    land_use_code = models.CharField(max_length=16, blank=True, db_index=True)
    land_use_description = models.CharField(max_length=128, blank=True)
    acreage = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    land_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    source_file = models.CharField(max_length=255, blank=True, db_default="")
    ingested_at = models.DateTimeField(auto_now_add=True, db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prop_id", "tax_year", "land_seq"],
                name="brazoscad_land_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["tax_year", "prop_id"]),
        ]
        verbose_name = "BCAD Property Land"
        verbose_name_plural = "BCAD Property Lands"

    def __str__(self) -> str:
        return f"PropertyLand(prop_id={self.prop_id}, year={self.tax_year}, seq={self.land_seq})"


class PropertyImprovement(models.Model):
    """Maps to BCAD APPRAISAL_IMPROVEMENT_INFO.TXT — improvement summary per account."""

    # imp_id is NOT globally unique on its own — a handful of real records
    # reuse the same imp_id across different prop_ids. (prop_id, imp_id,
    # tax_year) is confirmed unique against the real export.
    prop_id = models.CharField(max_length=32, db_index=True)
    imp_id = models.CharField(max_length=32, db_index=True)
    tax_year = models.PositiveIntegerField(db_index=True)

    improvement_type = models.CharField(max_length=16, blank=True, db_index=True)
    improvement_description = models.CharField(max_length=128, blank=True)
    # Not populated by load_brazos_cad — confirmed zero on every real row in
    # APPRAISAL_IMPROVEMENT_INFO.TXT; improvement dollar value isn't exported
    # there at all.
    improvement_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    # Not present in APPRAISAL_IMPROVEMENT_INFO.TXT — populated via a
    # post-ingest rollup from PropertyImprovementDetail.year_built (the detail
    # row with the largest detail_value per imp_id), not sourced directly.
    year_built = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    # Not populated — semantics of the source field are unconfirmed (some
    # implausible outlier values observed).
    square_feet = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    source_file = models.CharField(max_length=255, blank=True, db_default="")
    ingested_at = models.DateTimeField(auto_now_add=True, db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prop_id", "imp_id", "tax_year"], name="brazoscad_improvement_unique"
            ),
        ]
        indexes = [
            models.Index(fields=["tax_year", "prop_id"]),
        ]
        verbose_name = "BCAD Property Improvement"
        verbose_name_plural = "BCAD Property Improvements"

    def __str__(self) -> str:
        return f"PropertyImprovement(imp_id={self.imp_id}, year={self.tax_year})"


class PropertyImprovementDetail(models.Model):
    """Maps to BCAD APPRAISAL_IMPROVEMENT_DETAIL.TXT — line items per improvement."""

    # (imp_id, detail_seq) alone is NOT unique — a real handful of rows collide
    # across different prop_ids. (prop_id, imp_id, tax_year, detail_seq) is
    # confirmed unique against the real export.
    prop_id = models.CharField(max_length=32, db_index=True)
    imp_id = models.CharField(max_length=32, db_index=True)
    tax_year = models.PositiveIntegerField(db_index=True)
    detail_seq = models.IntegerField(default=0, db_index=True)

    detail_description = models.CharField(max_length=255, blank=True)
    detail_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    detail_quantity = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # Free-text tail field, not a short unit code — real content runs up to
    # ~500 chars (construction/perimeter codes like "R30,U60,L30,D60").
    detail_unit = models.TextField(blank=True)

    source_file = models.CharField(max_length=255, blank=True, db_default="")
    ingested_at = models.DateTimeField(auto_now_add=True, db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prop_id", "imp_id", "tax_year", "detail_seq"],
                name="brazoscad_improvement_detail_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["tax_year", "imp_id"]),
        ]
        verbose_name = "BCAD Property Improvement Detail"
        verbose_name_plural = "BCAD Property Improvement Details"

    def __str__(self) -> str:
        return f"PropertyImprovementDetail(imp_id={self.imp_id}, year={self.tax_year}, seq={self.detail_seq})"


class PropertyBuildingCharacteristic(models.Model):
    """Aggregated per-improvement building characteristics, pre-aggregated
    from APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT's attribute-type/value pairs
    (see counties/brazos/parsers/pacs.py) into one row per improvement -- wide/
    column-shaped, matching Harris's counties.harris.models.BuildingDetail, since the
    source file is attribute-pair shaped (long) but comps-similarity
    scoring needs columns to compare (wayfinder ticket #9).

    When multiple detail rows within one improvement carry the same
    attribute type -- rare, ~500/94,291 improvements, excluding the
    genuinely-multivalued types that live in PropertyExtraFeature instead
    -- the first value encountered in file order wins. This is a
    deterministic tiebreak for rare data noise, not a semantic rollup like
    year_built's highest-detail_value pick on PropertyImprovement.

    No bedroom/bathroom-adjacent "quality" or "condition" rating exists
    here -- see PropertyAccount.class_code (GIS-sourced) as the closest
    available proxy, and CLAUDE.md's Similarity Algorithm section for how
    Harris's own quality_code/condition_code are used.
    """

    prop_id = models.CharField(max_length=32, db_index=True)
    imp_id = models.CharField(max_length=32, db_index=True)
    tax_year = models.PositiveIntegerField(db_index=True)

    bedrooms = models.IntegerField(null=True, blank=True)
    # "Plumbing" attribute values are messy free text (e.g. "2", "2/1",
    # "2.5", "2 1/2", "3-1/2", "2,1/2") -- parsed by the loader into full +
    # half bath counts; unparseable values leave both null rather than guess.
    bathrooms = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    half_baths = models.IntegerField(null=True, blank=True)
    room_count = models.IntegerField(null=True, blank=True)

    exterior_wall = models.CharField(max_length=64, blank=True)
    foundation = models.CharField(max_length=64, blank=True)
    roof_covering = models.CharField(max_length=64, blank=True)
    heating_cooling = models.CharField(max_length=64, blank=True)
    interior_finish = models.CharField(max_length=64, blank=True)
    construction_style = models.CharField(max_length=64, blank=True)
    flooring = models.CharField(max_length=64, blank=True)

    source_file = models.CharField(max_length=255, blank=True, db_default="")
    ingested_at = models.DateTimeField(auto_now_add=True, db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prop_id", "imp_id", "tax_year"],
                name="brazoscad_building_characteristic_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["tax_year", "prop_id"]),
        ]
        verbose_name = "BCAD Property Building Characteristic"
        verbose_name_plural = "BCAD Property Building Characteristics"

    def __str__(self) -> str:
        return f"PropertyBuildingCharacteristic(imp_id={self.imp_id}, year={self.tax_year})"


class PropertyExtraFeature(models.Model):
    """One row per genuinely-multivalued attribute from
    APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT: fireplaces, covered patios/decks,
    carports, outdoor kitchens, pools, built-ins, and free-text "Other
    Feature" entries. feature_value is genuinely limited to 10 characters on
    the real export -- "Other Feature" values are truncated there by BCAD
    itself (e.g. "STORAGE BU", not "STORAGE BUILDING"), not by this model.
    Unlike PropertyBuildingCharacteristic, these are NOT aggregated to one row per
    improvement -- a single improvement can genuinely have several distinct
    values of the same type (two different "Other Feature" entries is
    common in the real export). No unique constraint: duplicates here are
    real, independent feature entries, not a data-quality collision to
    resolve, mirroring how counties.harris.models.ExtraFeature is Harris's own
    per-feature-row model (pools/garages/patios) alongside its single-row
    BuildingDetail.
    """

    prop_id = models.CharField(max_length=32, db_index=True)
    imp_id = models.CharField(max_length=32, db_index=True)
    tax_year = models.PositiveIntegerField(db_index=True)
    detail_seq = models.IntegerField(default=0, db_index=True)

    feature_type = models.CharField(max_length=64, db_index=True)
    feature_value = models.CharField(max_length=64, blank=True)

    source_file = models.CharField(max_length=255, blank=True, db_default="")
    ingested_at = models.DateTimeField(auto_now_add=True, db_default=Now())

    class Meta:
        indexes = [
            models.Index(fields=["tax_year", "prop_id"]),
            models.Index(fields=["feature_type"]),
        ]
        verbose_name = "BCAD Property Extra Feature"
        verbose_name_plural = "BCAD Property Extra Features"

    def __str__(self) -> str:
        return f"PropertyExtraFeature(imp_id={self.imp_id}, type={self.feature_type})"


class PropertyEntity(models.Model):
    """Maps to BCAD APPRAISAL_ENTITY.TXT — a bare entity_id -> entity_code lookup,
    not an entity master. Trimmed to exactly what that file provides (wayfinder
    ticket #9): tax-unit name/rate data now lives solely in the shared
    TaxUnitRate table (data/models.py, county="brazos") so there's one source of
    truth for rates, not two. NOT currently populated by load_brazos_cad.
    """

    entity_id = models.CharField(max_length=32, db_index=True)
    tax_year = models.PositiveIntegerField(db_index=True)
    entity_code = models.CharField(max_length=16, blank=True, db_index=True)

    source_file = models.CharField(max_length=255, blank=True, db_default="")
    ingested_at = models.DateTimeField(auto_now_add=True, db_default=Now())

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity_id", "tax_year"], name="brazoscad_entity_unique"
            ),
        ]
        indexes = [
            models.Index(fields=["tax_year", "entity_id"]),
        ]
        verbose_name = "BCAD Property Entity"
        verbose_name_plural = "BCAD Property Entities"

    def __str__(self) -> str:
        return f"PropertyEntity(entity_id={self.entity_id}, year={self.tax_year})"
