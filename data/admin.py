from __future__ import annotations

from typing import Any, cast

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone

from .etl_pipeline import lock as pipeline_lock
from .models import (
    AssessmentHistory,
    BuildingDetail,
    DownloadRecord,
    ExtraFeature,
    PropertyJurisdictionExemption,
    PropertyRecord,
    TaxUnitRate,
)
from .tasks_new import (
    download_and_import_building_data,
    download_and_import_gis_data,
    run_etl_pipeline,
)

admin.site.site_header = "Home Values Admin"
admin.site.site_title = "Home Values Admin"
admin.site.index_title = "ETL & Data Administration"


class ETLPipelineAdminForm(forms.Form):
    data_year = forms.IntegerField(
        label="HCAD data year",
        min_value=2020,
        required=False,
        initial=timezone.now().year,
        help_text="Leave blank to use the pipeline's configured default year.",
    )
    skip_download = forms.BooleanField(
        label="Skip download",
        required=False,
        help_text="Re-use previously downloaded archive files (faster; skips network I/O).",
    )
    skip_extract = forms.BooleanField(
        label="Skip extract",
        required=False,
        help_text="Re-use previously extracted files (skip ZIP extraction).",
    )
    skip_load = forms.BooleanField(
        label="Skip load (dry run)",
        required=False,
        help_text="Run download/extract stages only; do not write to the database.",
    )


@admin.register(DownloadRecord)
class DownloadRecordAdmin(admin.ModelAdmin):
    list_display = ("filename", "url", "downloaded_at", "extracted")
    list_filter = ("extracted",)
    search_fields = ("filename", "url")
    ordering = ("-downloaded_at",)
    readonly_fields = ("downloaded_at",)
    change_list_template = "admin/data/downloadrecord/change_list.html"

    def get_urls(self):
        app, model = self.model._meta.app_label, self.model._meta.model_name

        def n(suffix: str) -> str:
            return f"{app}_{model}_{suffix}"

        custom_urls = [
            path(
                "etl-pipeline/",
                self.admin_site.admin_view(self.etl_pipeline_view),
                name=n("etl_pipeline"),
            ),
            path(
                "trigger-gis-import/",
                self.admin_site.admin_view(self.trigger_gis_import_view),
                name=n("trigger_gis_import"),
            ),
            path(
                "trigger-building-import/",
                self.admin_site.admin_view(self.trigger_building_import_view),
                name=n("trigger_building_import"),
            ),
            path(
                "task-status/<str:task_id>/",
                self.admin_site.admin_view(self.task_status_view),
                name=n("task_status"),
            ),
        ]
        return custom_urls + super().get_urls()

    def _trigger_import_task(
        self,
        request: HttpRequest,
        task_func: Any,
        task_type: str,
        duration_estimate: str,
    ) -> HttpResponse:
        """Queue an import Celery task and redirect back to the changelist."""
        if not request.user.is_superuser:
            raise PermissionDenied("Only superusers may trigger ETL pipeline runs.")

        changelist_url = reverse("admin:data_downloadrecord_changelist")
        if request.method != "POST":
            return HttpResponseRedirect(changelist_url)

        running = pipeline_lock.current_run()
        if running:
            messages.warning(
                request,
                f"An ETL pipeline run (scope={running['scope']}) is already in progress; "
                "please wait for it to finish before starting another.",
            )
            return HttpResponseRedirect(changelist_url)

        task = cast(Any, task_func).delay()
        request.session["etl_last_task_id"] = task.id
        request.session["etl_last_task_type"] = task_type
        messages.success(
            request,
            f"{task_type} task queued (ID: {task.id}). This may take {duration_estimate} to complete.",
        )
        return HttpResponseRedirect(changelist_url)

    def trigger_gis_import_view(self, request: HttpRequest) -> HttpResponse:
        return self._trigger_import_task(
            request, download_and_import_gis_data, "GIS Import", "30+ minutes"
        )

    def trigger_building_import_view(self, request: HttpRequest) -> HttpResponse:
        return self._trigger_import_task(
            request, download_and_import_building_data, "Building Import", "15+ minutes"
        )

    def task_status_view(self, request: HttpRequest, task_id: str) -> JsonResponse:
        """Return JSON Celery task state for frontend polling."""
        if not request.user.is_superuser:
            raise PermissionDenied("Only superusers may view ETL task status.")

        from celery.result import AsyncResult

        result = AsyncResult(task_id)
        data: dict[str, Any] = {"task_id": task_id, "state": result.state}
        if isinstance(result.info, dict):
            data["step"] = result.info.get("step", "")
        elif isinstance(result.info, Exception):
            data["error"] = str(result.info)
        if result.state == "SUCCESS" and isinstance(result.result, dict):
            data["result"] = result.result
        return JsonResponse(data)

    def etl_pipeline_view(self, request: HttpRequest) -> HttpResponse:
        """Admin-only page to queue a full ETL pipeline run."""
        if not request.user.is_superuser:
            raise PermissionDenied("Only superusers may trigger or view ETL pipeline runs.")

        pipeline_url = reverse("admin:data_downloadrecord_etl_pipeline")
        changelist_url = reverse("admin:data_downloadrecord_changelist")

        if request.method == "POST":
            form = ETLPipelineAdminForm(request.POST)
            if form.is_valid():
                running = pipeline_lock.current_run()
                if running:
                    messages.warning(
                        request,
                        f"An ETL pipeline run (scope={running['scope']}) is already in progress; "
                        "please wait for it to finish before starting another.",
                    )
                    return HttpResponseRedirect(changelist_url)

                data_year = form.cleaned_data.get("data_year") or None
                skip_download = form.cleaned_data.get("skip_download", False)
                skip_extract = form.cleaned_data.get("skip_extract", False)
                skip_load = form.cleaned_data.get("skip_load", False)
                task = cast(Any, run_etl_pipeline).delay(
                    skip_download=skip_download,
                    skip_extract=skip_extract,
                    skip_load=skip_load,
                    data_year=data_year,
                )
                request.session["etl_last_task_id"] = task.id
                request.session["etl_last_task_type"] = "Full ETL Pipeline"
                messages.success(
                    request,
                    f"Queued ETL pipeline task {task.id} "
                    f"(skip_download={skip_download}, skip_extract={skip_extract}, skip_load={skip_load}).",
                )
                return HttpResponseRedirect(changelist_url)
        else:
            form = ETLPipelineAdminForm(initial={"data_year": timezone.now().year})

        # Build a URL template the JS can use for polling by replacing a placeholder
        task_status_url_template = reverse(
            "admin:data_downloadrecord_task_status",
            args=["TASK_ID_PLACEHOLDER"],
        )

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Re-download and run ETL pipeline",
            "form": form,
            "media": self.media + form.media,
            "changelist_url": changelist_url,
            "last_task_id": request.session.get("etl_last_task_id"),
            "last_task_type": request.session.get("etl_last_task_type"),
            "task_status_url_template": task_status_url_template,
            "current_run": pipeline_lock.current_run(),
        }
        return TemplateResponse(
            request,
            "admin/data/downloadrecord/etl_pipeline.html",
            context,
        )


@admin.register(PropertyRecord)
class PropertyRecordAdmin(admin.ModelAdmin):
    list_display = (
        "account_number",
        "address",
        "city",
        "zipcode",
        "value",
        "is_residential",
        "is_data_ready",
        "updated_at",
    )
    # address/zipcode/owner_name use plain icontains: they're pg_trgm GIN-indexed
    # (see migration 0011_propertyrecord_trigram_search_indexes), so an unanchored
    # search is cheap and mid-string matches (e.g. "Main St") stay searchable.
    # account_number has no trigram index, only a B-tree one, which an unanchored
    # LIKE '%...%' can't use — anchor it like AssessmentHistoryAdmin does below.
    search_fields = ("address", "zipcode", "^account_number", "owner_name")
    list_filter = ("is_residential", "is_data_ready", "state_class", "city", "zipcode")
    readonly_fields = ("created_at", "updated_at")
    date_hierarchy = "updated_at"
    # ~1.6M rows (see docs/guides/DATABASE.md) — skip the COUNT(*) query for pagination.
    show_full_result_count = False


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
    show_full_result_count = False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("property")

    fieldsets = (
        (
            "Property Link",
            {
                "fields": ("property", "account_number"),
            },
        ),
        (
            "Building Information",
            {
                "fields": (
                    "building_number",
                    "building_type",
                    "building_style",
                    "building_class",
                ),
            },
        ),
        (
            "Quality & Condition",
            {
                "fields": (
                    "quality_code",
                    "condition_code",
                ),
            },
        ),
        (
            "Age",
            {
                "fields": (
                    "year_built",
                    "year_remodeled",
                    "effective_year",
                ),
            },
        ),
        (
            "Areas & Stories",
            {
                "fields": (
                    "heat_area",
                    "base_area",
                    "gross_area",
                    "stories",
                ),
            },
        ),
        (
            "Construction",
            {
                "fields": (
                    "foundation_type",
                    "exterior_wall",
                    "roof_cover",
                    "roof_type",
                ),
            },
        ),
        (
            "Rooms",
            {
                "fields": (
                    "bedrooms",
                    "bathrooms",
                    "half_baths",
                    "fireplaces",
                ),
            },
        ),
        (
            "Import Metadata",
            {
                "fields": (
                    "is_active",
                    "import_date",
                    "import_batch_id",
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
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
    show_full_result_count = False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related("property")

    fieldsets = (
        (
            "Property Link",
            {
                "fields": ("property", "account_number"),
            },
        ),
        (
            "Feature Information",
            {
                "fields": (
                    "feature_number",
                    "feature_code",
                    "feature_description",
                ),
            },
        ),
        (
            "Measurements",
            {
                "fields": (
                    "quantity",
                    "area",
                    "length",
                    "width",
                ),
            },
        ),
        (
            "Quality & Condition",
            {
                "fields": (
                    "quality_code",
                    "condition_code",
                    "year_built",
                ),
            },
        ),
        (
            "Value",
            {
                "fields": ("value",),
            },
        ),
        (
            "Import Metadata",
            {
                "fields": (
                    "is_active",
                    "import_date",
                    "import_batch_id",
                    "created_at",
                    "updated_at",
                ),
                "classes": ("collapse",),
            },
        ),
    )


class ReadOnlyImportedDataAdminMixin:
    """Blocks add/change/delete through the admin UI.

    These two models are only ever populated by `import_tax_unit_rates` / `import_jur_exemptions`
    and feed directly into ARB hearing evidence numbers — a hand-edit or delete through the admin
    would silently diverge from the source TSV with no audit trail. Corrections must go through
    re-running the import command, not admin editing.
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AssessmentHistory)
class AssessmentHistoryAdmin(ReadOnlyImportedDataAdminMixin, admin.ModelAdmin):
    """Read-only visibility into per-year assessed/appraised/market value history — this is
    the genuine history of record (BuildingDetail/ExtraFeature are snapshot-only and get
    truncated/reloaded on every ETL run). Populated only by `import_assessment_history`;
    corrections must go through re-running that command, not admin editing."""

    list_display = (
        "account_number",
        "tax_year",
        "assessed_value",
        "appraised_value",
        "market_value",
        "cap_account",
        "updated_at",
    )
    list_filter = ("tax_year",)
    # "^" (startswith) avoids an icontains full-table scan on this multi-million-row history table.
    search_fields = ("^account_number", "^cap_account")
    ordering = ("-tax_year", "account_number")
    show_full_result_count = False


@admin.register(TaxUnitRate)
class TaxUnitRateAdmin(ReadOnlyImportedDataAdminMixin, admin.ModelAdmin):
    """Read-only visibility into adopted tax rates — there is no automated source for this
    data (HCAD doesn't publish it), so an operator needs to be able to see at a glance which
    tax years have rates on file before trusting a protest report's tax-impact numbers."""

    list_display = (
        "tax_unit_code",
        "tax_unit_name",
        "tax_year",
        "adopted_rate",
        "source",
        "updated_at",
    )
    list_filter = ("tax_year",)
    search_fields = ("tax_unit_code", "tax_unit_name")


@admin.register(PropertyJurisdictionExemption)
class PropertyJurisdictionExemptionAdmin(ReadOnlyImportedDataAdminMixin, admin.ModelAdmin):
    """Read-only visibility into imported jurisdiction/exemption rows, for the same reason
    as TaxUnitRateAdmin — this table is only refreshed on a manual/best-effort basis."""

    list_display = (
        "account_number",
        "tax_year",
        "tax_unit_code",
        "exemption_code",
        "taxable_value",
        "source",
        "updated_at",
    )
    list_filter = ("tax_year", "source")
    search_fields = ("account_number", "tax_unit_code")
    show_full_result_count = False
