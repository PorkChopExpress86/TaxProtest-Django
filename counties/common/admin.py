from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from counties.common.import_writers import RecoveryRejected, recover_writer, writer_status
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation


class WriterRecoveryForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea, label="Recovery reason", strip=True)


class ImportAuditInline(admin.TabularInline):
    model = ImportAuditEntry
    readonly_fields = ("kind", "actor", "reason", "created_at", "evidence", "result")
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("data.view_importoperation")


@admin.register(ImportOperation)
class ImportOperationAdmin(admin.ModelAdmin):
    inlines = (ImportAuditInline,)
    change_form_template = "admin/imports/operation.html"
    list_display = (
        "id",
        "county",
        "intent",
        "requested_year",
        "status",
        "county_writer",
        "started_at",
    )
    list_filter = ("county", "status", "intent")
    search_fields = ("id", "actor", "origin", "intent")
    readonly_fields = (
        *tuple(field.name for field in ImportOperation._meta.fields),
        "county_writer",
    )
    empty_value_display = "Not recorded"
    actions = None

    def get_urls(self):
        return [
            path(
                "<uuid:operation_id>/recover-writer/",
                self.admin_site.admin_view(self.recover_writer_view),
                name="data_importoperation_recover_writer",
            )
        ] + super().get_urls()

    def change_view(self, request, object_id, form_url="", extra_context=None):
        obj = self.get_object(request, object_id)
        context = dict(extra_context or {})
        if (
            obj
            and request.user.has_perm("data.recover_county_writer")
            and writer_status(obj.county).startswith("Recovery required")
        ):
            context["recovery_url"] = reverse(
                "admin:data_importoperation_recover_writer", args=[obj.pk]
            )
        return super().change_view(request, object_id, form_url, context)

    def recover_writer_view(self, request, operation_id):
        if not self.has_view_permission(request) or not request.user.has_perm(
            "data.recover_county_writer"
        ):
            raise PermissionDenied
        operation = get_object_or_404(ImportOperation, pk=operation_id)
        form = WriterRecoveryForm(request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            try:
                recover_writer(
                    operation, actor=request.user.get_username(), reason=form.cleaned_data["reason"]
                )
            except RecoveryRejected as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Writer reservation safely released")
                return redirect("admin:data_importoperation_change", operation.pk)
        return TemplateResponse(
            request,
            "admin/imports/recovery.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "Recover county writer",
                "operation": operation,
                "writer_status": writer_status(operation.county),
                "form": form,
            },
        )

    @admin.display(description="County writer")
    def county_writer(self, obj):
        return writer_status(obj.county)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ImportCandidate)
class ImportCandidateAdmin(admin.ModelAdmin):
    list_display = ("id", "county", "state", "created_at")
    list_filter = ("county", "state")
    readonly_fields = tuple(field.name for field in ImportCandidate._meta.fields)
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
