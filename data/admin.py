from django.contrib import admin
from django.contrib import messages
from .models import DownloadRecord


def trigger_gis_import(modeladmin, request, queryset):
    """Admin action to manually trigger GIS data import"""
    from .tasks import download_and_import_gis_data
    
    task = download_and_import_gis_data.delay()
    messages.success(
        request,
        f'GIS import task started with ID: {task.id}. This may take 30+ minutes to complete.'
    )


def trigger_building_import(modeladmin, request, queryset):
    """Admin action to manually trigger building data import"""
    from .tasks import download_and_import_building_data
    
    task = download_and_import_building_data.delay()
    messages.success(
        request,
        f'Building data import task started with ID: {task.id}. This may take 15+ minutes to complete.'
    )


trigger_gis_import.short_description = "Trigger GIS location data import (manual)"
trigger_building_import.short_description = "Trigger building data import (manual)"


@admin.register(DownloadRecord)
class DownloadRecordAdmin(admin.ModelAdmin):
    list_display = ('filename', 'url', 'downloaded_at', 'extracted')
    readonly_fields = ('downloaded_at',)
    actions = [trigger_gis_import, trigger_building_import]
from django.contrib import admin
from .models import PropertyRecord, BuildingDetail, ExtraFeature


@admin.register(PropertyRecord)
class PropertyRecordAdmin(admin.ModelAdmin):
    list_display = ("address", "zipcode", "value", "updated_at")
    search_fields = ("address", "zipcode", "account_number")
    list_filter = ("city", "zipcode")


@admin.register(BuildingDetail)
class BuildingDetailAdmin(admin.ModelAdmin):
    list_display = (
        "account_number",
        "building_number",
        "building_type",
        "year_built",
        "heat_area",
        "bedrooms",
        "bathrooms",
        "is_active",
        "import_date",
        "import_batch_id",
    )
    list_filter = (
        "is_active",
        "import_date",
        "building_type",
        "building_style",
    )
    search_fields = (
        "account_number",
        "property__address",
        "import_batch_id",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "import_date",
    )
    date_hierarchy = "import_date"
    
    fieldsets = (
        ("Property Link", {
            "fields": ("property", "account_number"),
        }),
        ("Building Information", {
            "fields": (
                "building_number",
                "building_type",
                "building_style",
                "building_class",
            ),
        }),
        ("Quality & Condition", {
            "fields": (
                "quality_code",
                "condition_code",
            ),
        }),
        ("Age", {
            "fields": (
                "year_built",
                "year_remodeled",
                "effective_year",
            ),
        }),
        ("Areas & Stories", {
            "fields": (
                "heat_area",
                "base_area",
                "gross_area",
                "stories",
            ),
        }),
        ("Construction", {
            "fields": (
                "foundation_type",
                "exterior_wall",
                "roof_cover",
                "roof_type",
            ),
        }),
        ("Rooms", {
            "fields": (
                "bedrooms",
                "bathrooms",
                "half_baths",
                "fireplaces",
            ),
        }),
        ("Import Metadata", {
            "fields": (
                "is_active",
                "import_date",
                "import_batch_id",
                "created_at",
                "updated_at",
            ),
            "classes": ("collapse",),
        }),
    )


@admin.register(ExtraFeature)
class ExtraFeatureAdmin(admin.ModelAdmin):
    list_display = (
        "account_number",
        "feature_code",
        "feature_description",
        "quantity",
        "area",
        "value",
        "is_active",
        "import_date",
        "import_batch_id",
    )
    list_filter = (
        "is_active",
        "import_date",
        "feature_code",
    )
    search_fields = (
        "account_number",
        "feature_code",
        "feature_description",
        "property__address",
        "import_batch_id",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "import_date",
    )
    date_hierarchy = "import_date"
    
    fieldsets = (
        ("Property Link", {
            "fields": ("property", "account_number"),
        }),
        ("Feature Information", {
            "fields": (
                "feature_number",
                "feature_code",
                "feature_description",
            ),
        }),
        ("Measurements", {
            "fields": (
                "quantity",
                "area",
                "length",
                "width",
            ),
        }),
        ("Quality & Condition", {
            "fields": (
                "quality_code",
                "condition_code",
                "year_built",
            ),
        }),
        ("Value", {
            "fields": ("value",),
        }),
        ("Import Metadata", {
            "fields": (
                "is_active",
                "import_date",
                "import_batch_id",
                "created_at",
                "updated_at",
            ),
            "classes": ("collapse",),
        }),
    )
