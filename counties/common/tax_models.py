"""Shared tax-data models: AssessmentHistory, TaxUnitRate, PropertyJurisdictionExemption,
ParcelGeometry.

These tables are the tax-impact and GIS data model reused verbatim across
counties (wayfinder ticket #9) — each carries a ``county`` column because
account_number/prop_id and tax_unit_code formats aren't guaranteed
collision-proof across counties, so ``county`` disambiguates real key
uniqueness rather than relying on incidental format differences.

They're defined here, in the county-neutral layer, rather than under a single
county's app. Each ``Meta.app_label`` is pinned to ``"data"`` (Harris's
pinned app label, see ``counties/harris/apps.py``) so the physical Python
module can move without moving the tables, migration history, or content
types — the same trick ``HarrisConfig.label`` already relies on to keep
``data_*`` table names stable across the original ``data`` ->
``counties.harris`` package move. Migrations for these models still live in
``counties/harris/migrations/``, since Django resolves migration history by
app label, not by where the model class's module happens to live.
"""

from django.db import models

COUNTY_CHOICES = [
    ("harris", "Harris"),
    ("brazos", "Brazos"),
]


class AssessmentHistory(models.Model):
    """Year-based assessed value history for real properties."""

    account_number = models.CharField(max_length=20, db_index=True)
    tax_year = models.IntegerField(db_index=True)
    county = models.CharField(
        max_length=16, choices=COUNTY_CHOICES, default="harris", db_index=True
    )
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
        app_label = "data"
        ordering = ["-tax_year"]
        constraints = [
            models.UniqueConstraint(
                fields=["account_number", "tax_year", "county"],
                name="unique_assessment_history_per_year",
            )
        ]

    def __str__(self):
        return f"{self.account_number} ({self.tax_year})"


class TaxUnitRate(models.Model):
    """Annual tax rate by taxing unit code."""

    tax_year = models.IntegerField(db_index=True)
    tax_unit_code = models.CharField(max_length=32, db_index=True)
    county = models.CharField(
        max_length=16, choices=COUNTY_CHOICES, default="harris", db_index=True
    )
    tax_unit_name = models.CharField(max_length=255, blank=True)
    adopted_rate = models.DecimalField(max_digits=12, decimal_places=8)
    source = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "data"
        constraints = [
            models.UniqueConstraint(
                fields=["tax_year", "tax_unit_code", "county"],
                name="unique_tax_rate_per_unit_year",
            )
        ]
        indexes = [
            models.Index(fields=["tax_year", "tax_unit_code"]),
        ]

    def __str__(self):
        return f"{self.tax_unit_code} {self.tax_year}: {self.adopted_rate}"


class PropertyJurisdictionExemption(models.Model):
    """Jurisdiction/exemption row linked to an account and year."""

    account_number = models.CharField(max_length=20, db_index=True)
    tax_year = models.IntegerField(db_index=True)
    county = models.CharField(
        max_length=16, choices=COUNTY_CHOICES, default="harris", db_index=True
    )
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
        app_label = "data"
        constraints = [
            models.UniqueConstraint(
                fields=["account_number", "tax_year", "tax_unit_code", "exemption_code", "county"],
                name="unique_jur_exemption_per_unit_code_year",
            )
        ]
        indexes = [
            models.Index(fields=["account_number", "tax_year"]),
            models.Index(fields=["tax_year", "tax_unit_code"]),
        ]

    def __str__(self):
        return f"{self.account_number} {self.tax_year} {self.tax_unit_code}"


class ParcelGeometry(models.Model):
    """Parcel coordinates (latitude, longitude) keyed by account.

    Lives in a standalone table rather than inline on the property model so
    the GIS ETL can INSERT at any time — it doesn't depend on the property
    table being populated first, and its download/extract overlaps the
    property sources' (see ``ETLOrchestrator._execute_download_extract``).
    The load stage itself still runs the sources one at a time; removing the
    ordering constraint is what makes loading them concurrently *possible*,
    not something the orchestrator does today.

    County-scoped via ``county`` (same convention as AssessmentHistory et al).
    The similarity query filters this table by bounding box, then joins to
    the property model by ``account_number``; measured against 1.17M rows the
    bounding-box scan costs the same as it did with inline columns (~115ms),
    with the join adding no measurable time — Postgres resolves it as a lazy
    nested-loop probe above the sort.
    """

    account_number = models.CharField(max_length=20, db_index=True)
    county = models.CharField(
        max_length=16, choices=COUNTY_CHOICES, default="harris", db_index=True
    )
    latitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True, db_index=True
    )
    longitude = models.DecimalField(
        max_digits=10, decimal_places=7, null=True, blank=True, db_index=True
    )
    # No parcel_id: the shapefiles carry no identifier distinct from the
    # account number. HCAD's parcel id column *is* HCAD_NUM, so the value was a
    # verbatim copy of account_number on all 1,546,749 Harris rows, and Brazos
    # wrote an empty string on all 77,235. Nothing read it. Re-add it only if a
    # source ever supplies a genuinely different identifier.
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "data"
        constraints = [
            models.UniqueConstraint(
                fields=["account_number", "county"],
                name="unique_parcel_geometry_per_account",
            )
        ]
        # Deliberately no composite (latitude, longitude) index. A btree can
        # only range-scan its leading column, so for a two-sided bounding box it
        # buys nothing over the single-column indexes above — measured on 1.17M
        # rows, Postgres declined to use it even as the only candidate and chose
        # a seq scan instead (227ms vs 117ms with the single-column indexes).

    def __str__(self):
        return f"{self.account_number} ({self.latitude}, {self.longitude})"
