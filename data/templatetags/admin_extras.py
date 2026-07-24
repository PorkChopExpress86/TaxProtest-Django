from django import template
from django.core.cache import cache
from django.db.models import Max
from django.utils import timezone

from data.models import (
    AssessmentHistory,
    DownloadRecord,
    PropertyJurisdictionExemption,
    PropertyRecord,
    TaxUnitRate,
)

register = template.Library()

DATA_HEALTH_CACHE_KEY = "admin:data_health_summary"
DATA_HEALTH_CACHE_SECONDS = 5 * 60


def _compute_data_health():
    current_year = timezone.now().year
    return {
        "downloads": list(
            DownloadRecord.objects.order_by("filename", "-downloaded_at").distinct("filename")
        ),
        "current_year": current_year,
        "tax_unit_rate_count": TaxUnitRate.objects.filter(tax_year=current_year).count(),
        "jurisdiction_exemption_count": PropertyJurisdictionExemption.objects.filter(
            tax_year=current_year
        ).count(),
        "not_ready_residential_count": PropertyRecord.objects.filter(
            is_residential=True, is_data_ready=False
        ).count(),
        "latest_assessment_history_year": AssessmentHistory.objects.aggregate(Max("tax_year"))[
            "tax_year__max"
        ],
    }


@register.inclusion_tag("admin/includes/data_health.html")
def data_health_summary():
    data = cache.get(DATA_HEALTH_CACHE_KEY)
    if data is None:
        data = _compute_data_health()
        cache.set(DATA_HEALTH_CACHE_KEY, data, DATA_HEALTH_CACHE_SECONDS)
    return data
