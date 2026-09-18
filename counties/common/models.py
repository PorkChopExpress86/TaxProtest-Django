"""Durable operator evidence; source interpretation remains county-owned."""

import uuid

from django.db import models

from counties.common.tax_models import COUNTY_CHOICES


class ImportOperation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    county = models.CharField(max_length=16, choices=COUNTY_CHOICES, db_index=True)
    intent = models.CharField(max_length=80)
    requested_year = models.IntegerField(null=True)
    origin = models.CharField(max_length=80, default="operator")
    actor = models.CharField(max_length=150, blank=True)
    status = models.CharField(max_length=32, default="running", db_index=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    publication_before = models.JSONField(null=True)
    publication_after = models.JSONField(null=True)
    evidence = models.JSONField(default=dict)
    warnings = models.JSONField(default=list)
    errors = models.JSONField(default=list)

    class Meta:
        app_label = "data"
        verbose_name = "import operation"
        verbose_name_plural = "Imports"
        ordering = ("-started_at",)
        default_permissions = ("view",)

    def __str__(self):
        return f"{self.get_county_display()} {self.intent}: {self.id}"


class CountyWriter(models.Model):
    county = models.CharField(max_length=16, choices=COUNTY_CHOICES, primary_key=True)
    operation = models.ForeignKey(ImportOperation, null=True, on_delete=models.PROTECT)
    backend_pid = models.IntegerField(null=True)

    class Meta:
        app_label = "data"
        default_permissions = ()
