from django.contrib import admin
from .models import DownloadRecord


@admin.register(DownloadRecord)
class DownloadRecordAdmin(admin.ModelAdmin):
    list_display = ('filename', 'url', 'downloaded_at', 'extracted')
    readonly_fields = ('downloaded_at',)
from django.contrib import admin
from .models import PropertyRecord


@admin.register(PropertyRecord)
class PropertyRecordAdmin(admin.ModelAdmin):
    list_display = ("address", "zipcode", "value", "updated_at")
    search_fields = ("address", "zipcode")
