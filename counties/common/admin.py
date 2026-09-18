from django.contrib import admin

from counties.common.models import ImportOperation


@admin.register(ImportOperation)
class ImportOperationAdmin(admin.ModelAdmin):
    list_display = ("id", "county", "intent", "requested_year", "status", "started_at")
    list_filter = ("county", "status", "intent")
    search_fields = ("id", "actor", "origin", "intent")
    readonly_fields = tuple(field.name for field in ImportOperation._meta.fields)
    empty_value_display = "Not recorded"
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
