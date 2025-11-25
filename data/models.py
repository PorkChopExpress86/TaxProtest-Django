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
    account_number = models.CharField(max_length=20, blank=True, db_index=True)
    owner_name = models.CharField(max_length=255, blank=True, db_index=True)
    assessed_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    building_area = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    land_area = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    street_number = models.CharField(max_length=16, blank=True)
    street_name = models.CharField(max_length=128, blank=True)

    # GIS fields
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True, db_index=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True, db_index=True)
    parcel_id = models.CharField(max_length=50, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.address} ({self.zipcode})"


class BuildingDetail(models.Model):
    """Residential building details imported from building_res.txt."""
    property = models.ForeignKey(PropertyRecord, on_delete=models.CASCADE, related_name='buildings')
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
    heat_area = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)  # Living area
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
            models.Index(fields=['account_number', 'building_number']),
            models.Index(fields=['is_active', 'import_date']),
        ]
    
    def __str__(self):
        return f"Building {self.building_number} for {self.account_number}"


class ExtraFeature(models.Model):
    """Extra features (pools, garages, etc.) imported from extra_features.txt."""
    property = models.ForeignKey(PropertyRecord, on_delete=models.CASCADE, related_name='extra_features')
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
            models.Index(fields=['account_number', 'feature_code']),
            models.Index(fields=['is_active', 'import_date']),
        ]
    
    def __str__(self):
        return f"{self.feature_description} for {self.account_number}"
