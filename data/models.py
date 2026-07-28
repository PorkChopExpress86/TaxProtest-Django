from decimal import Decimal

from django.db import models


class DownloadRecord(models.Model):
    """Tracks downloaded source files and whether they were extracted."""

    url = models.URLField()
    filename = models.CharField(max_length=512)
    downloaded_at = models.DateTimeField(auto_now_add=True)
    extracted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.filename} ({'extracted' if self.extracted else 'downloaded'})"


class PropertyRecord(models.Model):
    """Primary property table with core address/owner fields and HCAD attributes."""

    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100, blank=True)
    zipcode = models.CharField(max_length=20, blank=True)
    value = models.DecimalField(max_digits=14, decimal_places=2, null=True)
    source_url = models.TextField(blank=True)

    # Extended HCAD fields
    account_number = models.CharField(max_length=20, blank=True, db_index=True, unique=True)
    owner_name = models.CharField(max_length=255, blank=True, db_index=True)
    assessed_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    building_area = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    land_area = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    state_class = models.CharField(max_length=10, blank=True, db_index=True)
    is_residential = models.BooleanField(default=False, db_index=True)
    is_data_ready = models.BooleanField(default=False, db_index=True)
    street_number = models.CharField(max_length=16, blank=True)
    street_name = models.CharField(max_length=128, blank=True)

    # GIS fields
    latitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True, db_index=True
    )
    longitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True, db_index=True
    )
    parcel_id = models.CharField(max_length=50, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.address} ({self.zipcode})"


class BuildingDetail(models.Model):
    """Residential building details imported from building_res.txt."""

    property = models.ForeignKey(PropertyRecord, on_delete=models.CASCADE, related_name="buildings")
    account_number = models.CharField(max_length=20, db_index=True)

    # Building identification
    building_number = models.IntegerField(null=True, blank=True)
    building_type = models.CharField(max_length=10, blank=True)  # A1, A2, A3, A4, etc.
    building_style = models.CharField(max_length=10, blank=True)
    building_class = models.CharField(max_length=10, blank=True)

    # Quality and condition
    quality_code = models.CharField(max_length=10, blank=True)
    condition_code = models.CharField(max_length=10, blank=True)

    # Age
    year_built = models.IntegerField(null=True, blank=True, db_index=True)
    year_remodeled = models.IntegerField(null=True, blank=True)
    effective_year = models.IntegerField(null=True, blank=True)

    # Areas (square feet)
    heat_area = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )  # Living area
    base_area = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    gross_area = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # Stories
    stories = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)

    # Foundation and exterior
    foundation_type = models.CharField(max_length=10, blank=True)
    exterior_wall = models.CharField(max_length=10, blank=True)
    roof_cover = models.CharField(max_length=10, blank=True)
    roof_type = models.CharField(max_length=10, blank=True)

    # Room counts
    bedrooms = models.IntegerField(null=True, blank=True)
    bathrooms = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    half_baths = models.IntegerField(null=True, blank=True)

    # Other features
    fireplaces = models.IntegerField(null=True, blank=True)

    # Import metadata for tracking and soft deletes
    is_active = models.BooleanField(default=True, db_index=True)
    import_date = models.DateTimeField(null=True, blank=True, db_index=True)
    import_batch_id = models.CharField(max_length=50, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["account_number", "building_number"]),
            models.Index(fields=["is_active", "import_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account_number", "building_number"], name="unique_building_per_account"
            )
        ]

    def __str__(self):
        return f"Building {self.building_number} for {self.account_number}"


class ExtraFeature(models.Model):
    """Extra features (pools, garages, etc.) imported from extra_features.txt."""

    property = models.ForeignKey(
        PropertyRecord, on_delete=models.CASCADE, related_name="extra_features"
    )
    account_number = models.CharField(max_length=20, db_index=True)

    # Feature identification
    feature_number = models.IntegerField(null=True, blank=True)
    feature_code = models.CharField(max_length=10, db_index=True)  # Pool, garage, etc.
    feature_description = models.CharField(max_length=255, blank=True)

    # Feature details
    quantity = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    area = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    length = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    width = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    # Quality and condition
    quality_code = models.CharField(max_length=10, blank=True)
    condition_code = models.CharField(max_length=10, blank=True)
    year_built = models.IntegerField(null=True, blank=True)

    # Value
    value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    # Import metadata for tracking and soft deletes
    is_active = models.BooleanField(default=True, db_index=True)
    import_date = models.DateTimeField(null=True, blank=True, db_index=True)
    import_batch_id = models.CharField(max_length=50, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["account_number", "feature_code"]),
            models.Index(fields=["is_active", "import_date"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account_number", "feature_code", "feature_number"],
                name="unique_feature_per_account",
            )
        ]

    def __str__(self):
        return f"{self.feature_description} for {self.account_number}"


class AssessmentHistory(models.Model):
    """Year-based assessed value history for real properties."""

    account_number = models.CharField(max_length=20, db_index=True)
    tax_year = models.IntegerField(db_index=True)
    assessed_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    appraised_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    market_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    prior_appraised_value = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    prior_market_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    new_construction_value = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    cap_account = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-tax_year"]
        constraints = [
            models.UniqueConstraint(
                fields=["account_number", "tax_year"], name="unique_assessment_history_per_year"
            )
        ]

    def __str__(self):
        return f"{self.account_number} ({self.tax_year})"


class TaxUnitRate(models.Model):
    """Annual tax rate by taxing unit code."""

    tax_year = models.IntegerField(db_index=True)
    tax_unit_code = models.CharField(max_length=32, db_index=True)
    tax_unit_name = models.CharField(max_length=255, blank=True)
    adopted_rate = models.DecimalField(max_digits=12, decimal_places=8)
    source = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tax_year", "tax_unit_code"], name="unique_tax_rate_per_unit_year"
            )
        ]
        indexes = [
            models.Index(fields=["tax_year", "tax_unit_code"]),
        ]

    def __str__(self):
        return f"{self.tax_unit_code} {self.tax_year}: {self.adopted_rate}"


class PropertyJurisdictionExemption(models.Model):
    """HCAD jurisdiction/exemption row linked to an account and year."""

    account_number = models.CharField(max_length=20, db_index=True)
    tax_year = models.IntegerField(db_index=True)
    tax_unit_code = models.CharField(max_length=32, db_index=True)
    tax_unit_name = models.CharField(max_length=255, blank=True)
    exemption_code = models.CharField(max_length=32, blank=True)
    exemption_description = models.CharField(max_length=255, blank=True)
    exemption_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    exemption_percent = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    taxable_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    assessed_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    source = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account_number", "tax_year", "tax_unit_code", "exemption_code"],
                name="unique_jur_exemption_per_unit_code_year",
            )
        ]
        indexes = [
            models.Index(fields=["account_number", "tax_year"]),
            models.Index(fields=["tax_year", "tax_unit_code"]),
        ]

    def __str__(self):
        return f"{self.account_number} {self.tax_year} {self.tax_unit_code}"


# ---------------------------------------------------------------------------
# Brazos Central Appraisal District (PACS appraisal export)
#
# Loaded by `manage.py load_brazos_cad` from the fixed-width certified-roll
# files described in data/brazos_layouts.py, which owns the byte offsets.
#
# These tables are populated with PostgreSQL COPY, not the ORM, so a few
# deliberate departures from normal Django modelling apply throughout:
#
#   * `prop_id` / `imp_id` are plain indexed integers, not ForeignKey fields.
#     PACS exports contain orphan rows (improvements whose account is absent
#     from the same supplement), and a real FK would abort the entire bulk load
#     on the first dangling reference. Joins are done on these columns instead.
#   * Every table carries a surrogate BigAutoField primary key plus a
#     UniqueConstraint over its natural key. The natural key is what
#     `ON CONFLICT` targets, which is what makes re-importing a year idempotent.
#   * `created_at` / `updated_at` are set explicitly in SQL by the loader, since
#     COPY bypasses Django's auto_now_add/auto_now handling.
# ---------------------------------------------------------------------------


class BrazosImportRun(models.Model):
    """One execution of the Brazos CAD loader, for auditing and resumption.

    The `export_*` fields come from APPRAISAL_HEADER.TXT and pin down which
    export the rows came from. This matters because the district re-exports
    through the year as supplements are certified: two files both labelled 2025
    can carry different values for the same property, and without the header
    there is no way to tell which one a table currently holds.
    """

    tax_year = models.IntegerField(db_index=True)
    source_url = models.TextField(blank=True)
    archive_name = models.CharField(max_length=512, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=32, default="running", db_index=True)
    rows_loaded = models.BigIntegerField(default=0)
    rows_rejected = models.BigIntegerField(default=0)
    notes = models.TextField(blank=True)

    # Export provenance (APPRAISAL_HEADER.TXT)
    export_run_date = models.CharField(max_length=16, blank=True)
    export_description = models.CharField(max_length=40, blank=True)
    export_supplement_num = models.IntegerField(null=True, blank=True)
    export_dataset_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    export_pacs_version = models.CharField(max_length=10, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Brazos {self.tax_year} import ({self.status})"

    @property
    def is_certified_roll(self) -> bool:
        """True when this export is the certified roll rather than a supplement."""
        return self.export_supplement_num == 0


class PropertyAccount(models.Model):
    """Property/account record — APPRAISAL_INFO.TXT (PACS File #2)."""

    id = models.BigAutoField(primary_key=True)

    prop_id = models.BigIntegerField(db_index=True)
    prop_type_cd = models.CharField(max_length=5, blank=True, db_index=True)
    tax_year = models.IntegerField(db_index=True)
    sup_num = models.BigIntegerField(null=True, blank=True)

    geo_id = models.CharField(max_length=50, blank=True, db_index=True)
    py_owner_id = models.BigIntegerField(null=True, blank=True)
    owner_name = models.CharField(max_length=70, blank=True, db_index=True)
    owner_addr_line1 = models.CharField(max_length=60, blank=True)
    owner_addr_line2 = models.CharField(max_length=60, blank=True)
    owner_addr_line3 = models.CharField(max_length=60, blank=True)
    owner_addr_city = models.CharField(max_length=50, blank=True)
    owner_addr_state = models.CharField(max_length=50, blank=True)
    owner_addr_zip = models.CharField(max_length=5, blank=True)

    situs_num = models.CharField(max_length=15, blank=True, db_index=True)
    situs_unit = models.CharField(max_length=5, blank=True)
    situs_street_prefix = models.CharField(max_length=10, blank=True)
    situs_street = models.CharField(max_length=50, blank=True, db_index=True)
    situs_street_suffix = models.CharField(max_length=10, blank=True)
    situs_city = models.CharField(max_length=30, blank=True, db_index=True)
    situs_zip = models.CharField(max_length=10, blank=True, db_index=True)

    legal_desc = models.CharField(max_length=255, blank=True)
    legal_desc2 = models.CharField(max_length=255, blank=True)
    legal_acreage = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    abs_subdv_cd = models.CharField(max_length=10, blank=True, db_index=True)
    hood_cd = models.CharField(max_length=10, blank=True, db_index=True)
    block = models.CharField(max_length=50, blank=True)
    tract_or_lot = models.CharField(max_length=50, blank=True)

    # Value components. numeric(15) in the export; widened here so a malformed
    # or unusually large figure cannot overflow the column mid-COPY.
    land_hstd_val = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    land_non_hstd_val = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    imprv_hstd_val = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    imprv_non_hstd_val = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    ag_use_val = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    ag_market = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    timber_use = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    timber_market = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    # Raw export fields. Both are computed BEFORE the agricultural productivity
    # deduction, so on ag land they overstate value — appraised_val is really the
    # market value. Read the `market_value` / `appraised_value` / `assessed_value`
    # properties below instead of these.
    appraised_val = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True, db_index=True
    )
    ten_percent_cap = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    assessed_val = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True, db_index=True
    )
    # Post-productivity-loss figures; these match what the district publishes.
    appraised_val_prod_loss = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True, db_index=True
    )
    assessed_val_prod_loss = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True, db_index=True
    )
    # SB2 (2023) limitation. Beyond the published layout, so NULL on exports
    # that predate it rather than assumed zero.
    circuit_breaker_val = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True
    )

    arb_protest_flag = models.BooleanField(null=True, blank=True)
    deed_dt = models.CharField(max_length=25, blank=True)

    hs_exempt = models.BooleanField(null=True, blank=True)
    ov65_exempt = models.BooleanField(null=True, blank=True)
    dp_exempt = models.BooleanField(null=True, blank=True)

    imprv_state_cd = models.CharField(max_length=10, blank=True, db_index=True)
    land_state_cd = models.CharField(max_length=10, blank=True, db_index=True)
    personal_state_cd = models.CharField(max_length=10, blank=True)
    land_acres = models.DecimalField(max_digits=24, decimal_places=4, null=True, blank=True)

    # Comma-separated taxing units for this account ("C2, CAD, G1, S2"). The only
    # per-property jurisdiction data in the export.
    entities = models.CharField(max_length=140, blank=True)
    # Identifies the export this row came from; joins to BrazosImportRun.
    dataset_id = models.BigIntegerField(null=True, blank=True, db_index=True)

    # Parcel centroid, populated by `load_brazos_gis` from the district's
    # shapefile — the certified roll carries no spatial data at all. Only real
    # property (prop_type_cd 'R') has a boundary; mineral, personal-property and
    # mobile-home accounts stay NULL by nature, not by omission.
    latitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True, db_index=True
    )
    longitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True, db_index=True
    )
    parcel_area_sqft = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prop_id", "tax_year"], name="unique_brazos_account_per_year"
            )
        ]
        indexes = [
            models.Index(fields=["tax_year", "prop_id"]),
            models.Index(fields=["tax_year", "hood_cd"]),
            models.Index(fields=["tax_year", "situs_city"]),
            models.Index(fields=["tax_year", "situs_street", "situs_num"]),
            # Bounding-box prefilter for distance search.
            models.Index(fields=["tax_year", "latitude", "longitude"]),
        ]

    def __str__(self):
        return f"Brazos {self.prop_id} ({self.tax_year})"

    # -- Value accessors ----------------------------------------------------
    #
    # Use these rather than the raw columns. The export's `appraised_val` and
    # `assessed_val` are pre-productivity-loss, so on agricultural land they
    # overstate value by the ag deduction — often by an order of magnitude.

    @property
    def market_value(self):
        """Total market value: the sum of the land, improvement and ag components."""
        return self.appraised_val

    @property
    def appraised_value(self):
        """Appraised value after any agricultural productivity deduction."""
        if self.appraised_val_prod_loss is not None:
            return self.appraised_val_prod_loss
        return self.appraised_val

    @property
    def assessed_value(self):
        """Taxable assessed value, after ag loss, homestead cap and circuit breaker."""
        if self.assessed_val_prod_loss is not None:
            return self.assessed_val_prod_loss
        return self.assessed_val

    @property
    def ag_value_loss(self):
        """Productivity deduction: the gap between ag/timber market and use value."""
        zero = Decimal("0")
        return ((self.ag_market or zero) - (self.ag_use_val or zero)) + (
            (self.timber_market or zero) - (self.timber_use or zero)
        )

    @property
    def situs_address(self) -> str:
        """Street address as the district renders it, or '' when unknown.

        Space-separated with no '#' before the unit, matching how Brazos CAD
        prints it ("1640 BRIARCREST DR 100"). Format differently in templates if
        you want to; this property is the district's rendering, so it stays
        directly comparable to their published record.
        """
        return " ".join(
            part
            for part in (
                self.situs_num,
                self.situs_street_prefix,
                self.situs_street,
                self.situs_street_suffix,
                self.situs_unit,
            )
            if part
        ).strip()

    @property
    def taxing_units(self) -> list[str]:
        """`entities` split into individual taxing-unit codes."""
        return [unit.strip() for unit in self.entities.split(",") if unit.strip()]

    @property
    def has_location(self) -> bool:
        """True when this account has a parcel centroid for distance search."""
        return self.latitude is not None and self.longitude is not None


class PropertyLand(models.Model):
    """Land segment — APPRAISAL_LAND_DETAIL.TXT (PACS File #10)."""

    id = models.BigAutoField(primary_key=True)

    prop_id = models.BigIntegerField(db_index=True)
    tax_year = models.IntegerField(db_index=True)
    land_seg_id = models.BigIntegerField(db_index=True)

    land_type_cd = models.CharField(max_length=10, blank=True, db_index=True)
    land_type_desc = models.CharField(max_length=25, blank=True)
    state_cd = models.CharField(max_length=5, blank=True, db_index=True)
    land_seg_homesite = models.BooleanField(null=True, blank=True)

    size_acres = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    size_square_feet = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    effective_front = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    effective_depth = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    mkt_ls_method = models.CharField(max_length=5, blank=True)
    mkt_ls_class = models.CharField(max_length=10, blank=True)
    land_seg_mkt_val = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    ag_apply = models.BooleanField(null=True, blank=True)
    ag_ls_method = models.CharField(max_length=5, blank=True)
    ag_ls_class = models.CharField(max_length=10, blank=True)
    ag_value = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    land_homesite_pct = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prop_id", "tax_year", "land_seg_id"],
                name="unique_brazos_land_segment",
            )
        ]
        indexes = [
            models.Index(fields=["tax_year", "prop_id"]),
        ]

    def __str__(self):
        return f"Land {self.land_seg_id} on {self.prop_id} ({self.tax_year})"


class PropertyImprovement(models.Model):
    """Improvement — APPRAISAL_IMPROVEMENT_INFO.TXT (PACS File #7)."""

    id = models.BigAutoField(primary_key=True)

    prop_id = models.BigIntegerField(db_index=True)
    tax_year = models.IntegerField(db_index=True)
    imp_id = models.BigIntegerField(db_index=True)

    imprv_type_cd = models.CharField(max_length=10, blank=True, db_index=True)
    imprv_type_desc = models.CharField(max_length=25, blank=True)
    imprv_state_cd = models.CharField(max_length=5, blank=True, db_index=True)
    imprv_homesite = models.BooleanField(null=True, blank=True)
    imprv_val = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    imprv_homesite_pct = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    omitted = models.BooleanField(null=True, blank=True)
    omitted_imprv_val = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prop_id", "tax_year", "imp_id"],
                name="unique_brazos_improvement",
            )
        ]
        indexes = [
            models.Index(fields=["tax_year", "prop_id"]),
        ]

    def __str__(self):
        return f"Improvement {self.imp_id} on {self.prop_id} ({self.tax_year})"


class PropertyImprovementDetail(models.Model):
    """Improvement detail — APPRAISAL_IMPROVEMENT_DETAIL.TXT (PACS File #8)."""

    id = models.BigAutoField(primary_key=True)

    prop_id = models.BigIntegerField(db_index=True)
    tax_year = models.IntegerField(db_index=True)
    imp_id = models.BigIntegerField(db_index=True)
    imprv_det_id = models.BigIntegerField(db_index=True)

    imprv_det_type_cd = models.CharField(max_length=10, blank=True, db_index=True)
    imprv_det_type_desc = models.CharField(max_length=25, blank=True)
    imprv_det_class_cd = models.CharField(max_length=10, blank=True, db_index=True)
    yr_built = models.IntegerField(null=True, blank=True, db_index=True)
    depreciation_yr = models.IntegerField(null=True, blank=True)
    imprv_det_area = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True, db_index=True
    )
    imprv_det_val = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    # PACS sketch program describing the footprint the area was derived from.
    sketch_cmds = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["prop_id", "tax_year", "imp_id", "imprv_det_id"],
                name="unique_brazos_improvement_detail",
            )
        ]
        indexes = [
            models.Index(fields=["tax_year", "prop_id"]),
            models.Index(fields=["tax_year", "imp_id"]),
        ]

    def __str__(self):
        return f"Detail {self.imprv_det_id} on improvement {self.imp_id}"


class PropertyEntity(models.Model):
    """Taxing jurisdiction — APPRAISAL_ENTITY.TXT (PACS File #14)."""

    id = models.BigAutoField(primary_key=True)

    entity_id = models.BigIntegerField(db_index=True)
    entity_cd = models.CharField(max_length=5, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "property entities"
        constraints = [
            models.UniqueConstraint(fields=["entity_id"], name="unique_brazos_entity"),
        ]

    def __str__(self):
        return f"{self.entity_cd} ({self.entity_id})"
