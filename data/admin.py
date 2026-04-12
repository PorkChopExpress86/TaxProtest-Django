from __future__ import annotations

from typing import Any, cast

from django import forms
from django.contrib import admin, messages
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone

from .models import BuildingDetail, DownloadRecord, ExtraFeature, PropertyRecord
from .tasks_new import (
    download_and_import_building_data,
    download_and_import_gis_data,
    run_etl_pipeline,
)


class ETLPipelineAdminForm(forms.Form):
    data_year = forms.IntegerField(
        label="HCAD data year",
        min_value=2020,
        required=False,
        initial=timezone.now().year,
        help_text=(
            "Leave blank to use the pipeline's configured default year. "
            "The full download, extract, and load stages will run."
        ),
    )


@admin.action(description="Trigger GIS location data import (manual)")
def trigger_gis_import(modeladmin, request, queryset):
    """Admin action to manually trigger GIS data import."""
    task = cast(Any, download_and_import_gis_data).delay()
    messages.success(
        request,
        f"GIS import task started with ID: {task.id}. This may take 30+ minutes to complete.",
    )


@admin.action(description="Trigger building data import (manual)")
def trigger_building_import(modeladmin, request, queryset):
    """Admin action to manually trigger building data import."""
    task = cast(Any, download_and_import_building_data).delay()
    messages.success(
        request,
        f"Building import task started with ID: {task.id}. This may take 15+ minutes to complete.",
    )


@admin.register(DownloadRecord)
class DownloadRecordAdmin(admin.ModelAdmin):
    list_display = ("filename", "url", "downloaded_at", "extracted")
    readonly_fields = ("downloaded_at",)
    actions = [trigger_gis_import, trigger_building_import]
    change_list_template = "admin/data/downloadrecord/change_list.html"

    def get_urls(self):
        info = self.model._meta.app_label, self.model._meta.model_name
        custom_urls = [
            path(
                "etl-pipeline/",
                self.admin_site.admin_view(self.etl_pipeline_view),
                name=f"{info[0]}_{info[1]}_etl_pipeline",
            ),
        ]
        return custom_urls + super().get_urls()

    def etl_pipeline_view(self, request: HttpRequest) -> HttpResponse:
        """Admin-only page to queue a full ETL pipeline run."""
        changelist_url = reverse("admin:data_downloadrecord_changelist")

        if request.method == "POST":
            form = ETLPipelineAdminForm(request.POST)
            if form.is_valid():
                data_year = form.cleaned_data.get("data_year") or None
                task = cast(Any, run_etl_pipeline).delay(
                    skip_download=False,
                    skip_extract=False,
                    skip_load=False,
                    data_year=data_year,
                )
                messages.success(
                    request,
                    (
                        f"Queued full ETL pipeline task {task.id}. "
                        "The pipeline will re-download, re-extract, and load the configured HCAD sources."
                    ),
                )
                return HttpResponseRedirect(changelist_url)
        else:
            form = ETLPipelineAdminForm(initial={"data_year": timezone.now().year})

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Re-download and run ETL pipeline",
            "form": form,
            "media": self.media + form.media,
            "changelist_url": changelist_url,
        }
        return TemplateResponse(
            request,
            "admin/data/downloadrecord/etl_pipeline.html",
            context,
        )


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
    list_select_related = ("property",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("property")
    
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
    list_select_related = ("property",)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("property")
    
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
