from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html_join

from counties.common.import_review import (
    ImportReviewRejected,
    captured_binding,
    checked_binding,
    review_candidate,
)
from counties.common.import_writers import RecoveryRejected, recover_writer, writer_status
from counties.common.models import ImportAuditEntry, ImportCandidate, ImportOperation


class WriterRecoveryForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea, label="Recovery reason", strip=True)


class CandidateReviewForm(forms.Form):
    decision = forms.ChoiceField(
        choices=(("approved", "Approve coverage exception"), ("rejected", "Reject candidate"))
    )
    reason = forms.CharField(widget=forms.Textarea, label="Review justification", strip=True)
    binding = forms.CharField(widget=forms.HiddenInput)


class CandidateApplyForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea, label="Application reason", strip=True)


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
    change_form_template = "admin/imports/candidate.html"
    list_display = ("id", "county", "state", "created_at")
    list_filter = ("county", "state")
    readonly_fields = (
        *tuple(field.name for field in ImportCandidate._meta.fields),
        "review_history",
    )
    actions = None

    def get_urls(self):
        return [
            path(
                "<uuid:candidate_id>/apply/",
                self.admin_site.admin_view(self.apply_view),
                name="data_importcandidate_apply",
            ),
            path(
                "<uuid:candidate_id>/review/",
                self.admin_site.admin_view(self.review_view),
                name="data_importcandidate_review",
            ),
        ] + super().get_urls()

    def change_view(self, request, object_id, form_url="", extra_context=None):
        context = dict(extra_context or {})
        if request.user.has_perm("data.approve_import_coverage"):
            context["review_url"] = reverse("admin:data_importcandidate_review", args=[object_id])
            context["apply_url"] = reverse("admin:data_importcandidate_apply", args=[object_id])
        return super().change_view(request, object_id, form_url, context)

    @admin.display(description="Coverage review history — Approval does not publish")
    def review_history(self, obj):
        return (
            format_html_join(
                "",
                "<p>{} — {} — {} — {}</p>",
                (
                    (entry.created_at, entry.actor, entry.result, entry.reason)
                    for entry in obj.operation.audit_entries.filter(
                        kind="coverage_review"
                    ).order_by("created_at")
                ),
            )
            or "No review decisions recorded. Approval does not publish."
        )

    def review_view(self, request, candidate_id):
        if not self.has_view_permission(request) or not request.user.has_perm(
            "data.approve_import_coverage"
        ):
            raise PermissionDenied
        candidate = get_object_or_404(ImportCandidate, pk=candidate_id)
        review_error = None
        if request.method == "POST":
            form = CandidateReviewForm(request.POST)
            if form.is_valid():
                try:
                    review_candidate(
                        candidate,
                        user=request.user,
                        reason=form.cleaned_data["reason"],
                        decision=form.cleaned_data["decision"],
                        expected_binding=form.cleaned_data["binding"],
                    )
                except ImportReviewRejected as exc:
                    form.add_error(None, str(exc))
                else:
                    messages.success(
                        request, "Review decision recorded. Approval does not publish data."
                    )
                    return redirect("admin:data_importcandidate_change", candidate.pk)
        else:
            try:
                checked_binding(candidate)
            except ImportReviewRejected as exc:
                review_error = str(exc)
            form = CandidateReviewForm(initial={"binding": captured_binding(candidate)})
        return TemplateResponse(
            request,
            "admin/imports/review.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "Review exact import candidate",
                "candidate": candidate,
                "form": form,
                "review_error": review_error,
            },
        )

    def apply_view(self, request, candidate_id):
        if not self.has_view_permission(request) or not request.user.has_perm(
            "data.approve_import_coverage"
        ):
            raise PermissionDenied
        candidate = get_object_or_404(ImportCandidate, pk=candidate_id)
        form = CandidateApplyForm(request.POST if request.method == "POST" else None)
        if request.method == "POST" and form.is_valid():
            try:
                if candidate.county == "brazos":
                    from counties.brazos.annual_refresh import RefreshOptions
                    from counties.brazos.property_import import (
                        PropertyImportMode,
                        PropertyImportRequest,
                        build_default_property_import,
                    )

                    result = build_default_property_import(self).run(
                        PropertyImportRequest(
                            mode=PropertyImportMode(candidate.request["mode"]),
                            options=RefreshOptions(tax_year=candidate.request["tax_year"]),
                            candidate_id=candidate.pk,
                            actor=request.user.get_username(),
                            origin="admin",
                            application_reason=form.cleaned_data["reason"],
                        ),
                        reviewer=request.user,
                    )
                    if result.workflow_state not in ("published", "already_applied"):
                        raise ImportReviewRejected("Candidate was not applied")
                else:
                    self.apply_harris(candidate, request, form)
                    return redirect("admin:data_importcandidate_change", candidate.pk)
            except (ImportReviewRejected, ValueError) as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    (
                        "Candidate already applied; no new write."
                        if result.already_applied
                        else "Candidate publication observed. Data is applied."
                    ),
                )
                return redirect("admin:data_importcandidate_change", candidate.pk)
        return TemplateResponse(
            request,
            "admin/imports/apply.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": "Apply exact qualified candidate",
                "candidate": candidate,
                "form": form,
            },
        )

    def apply_harris(self, candidate, request, form):
        from counties.harris.etl_pipeline import HarrisImportRequest, run_harris_import
        from counties.harris.etl_pipeline.import_plan import HarrisImportPlan

        result = run_harris_import(
            HarrisImportRequest(
                plan=HarrisImportPlan.from_legacy_scope(candidate.request["plan"]),
                data_year=candidate.request["data_year"],
                candidate_id=candidate.pk,
                actor=request.user.get_username(),
                origin="admin",
                application_reason=form.cleaned_data["reason"],
            ),
            reviewer=request.user,
        )
        if not result.success:
            raise ImportReviewRejected("Candidate was not applied")
        messages.success(
            request,
            (
                "Candidate already applied; no new write."
                if result.already_applied
                else "Candidate publication observed. Data is applied."
            ),
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
