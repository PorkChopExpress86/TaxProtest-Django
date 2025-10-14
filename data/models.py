from django.db import models


class DownloadRecord(models.Model):
    url = models.URLField()
    filename = models.CharField(max_length=512)
    downloaded_at = models.DateTimeField(auto_now_add=True)
    extracted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.filename} ({'extracted' if self.extracted else 'downloaded'})"
from django.db import models


class PropertyRecord(models.Model):
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100, blank=True)
    zipcode = models.CharField(max_length=20, blank=True)
    value = models.DecimalField(max_digits=14, decimal_places=2, null=True)
    source_url = models.TextField(blank=True)

    # Extended HCAD fields
    account_number = models.CharField(max_length=20, blank=True, db_index=True)
    owner_name = models.CharField(max_length=255, blank=True, db_index=True)
    assessed_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    building_area = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    land_area = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    street_number = models.CharField(max_length=16, blank=True)
    street_name = models.CharField(max_length=128, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.address} ({self.zipcode})"
