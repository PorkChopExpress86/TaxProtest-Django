"""Shared tax-data models: AssessmentHistory, TaxUnitRate, PropertyJurisdictionExemption.

These three tables are the tax-impact data model reused verbatim across
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
