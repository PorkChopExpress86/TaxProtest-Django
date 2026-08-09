"""Bind a county adapter to the shared views and produce its URL patterns."""

from __future__ import annotations

from functools import partial

from django.urls import path

from counties.common import views
from counties.common.contracts import CountyAdapter

#: (view function, path suffix, URL-name suffix). ``<str:key>`` is the county's
#: own property identifier — an HCAD account number, a BCAD prop_id, and so on.
_ROUTES = (
    (views.index, "", "index"),
    (views.export_csv, "export/", "export_csv"),
    (views.similar_properties, "similar/<str:key>/", "similar_properties"),
    (views.protest_analysis, "protest/<str:key>/", "protest_analysis"),
    (views.protest_analysis_export, "protest/<str:key>/export/", "protest_analysis_export"),
    (views.protest_analysis_pdf, "protest/<str:key>/pdf/", "protest_analysis_pdf"),
)


def county_urlpatterns(adapter: CountyAdapter) -> list:
    """Every shared page for one county, named with that county's prefix.

    Harris mounts at the site root with unprefixed names (``index``,
    ``protest_analysis``); Brazos mounts under ``brazos/`` with ``brazos_``
    names. Both get the identical route set.
    """
    patterns = []
    for view, suffix, name in _ROUTES:
        if name == "export_csv" and not adapter.profile.supports_search_export:
            continue
        patterns.append(
            path(suffix, partial(view, adapter=adapter), name=adapter.profile.url_name(name))
        )
    return patterns
