"""The three property pages, rendered identically for every county.

Each view takes a :class:`~counties.common.contracts.CountyAdapter`, asks it for
neutral records, and does all analysis and presentation here.
``county_urlpatterns`` in :mod:`counties.common.urls` binds an adapter to these.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import wraps
from typing import Any

from django.core.paginator import Paginator
from django.db import connection, transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import render
from django.urls import reverse

from counties.common.analysis import (
    PROTEST_MAX_MIN_SCORE,
    PROTEST_MIN_MIN_SCORE,
    build_comparables_dossier,
    build_protest_dossier,
    clamped_float,
)
from counties.common.contracts import Comp, CountyAdapter, Subject
from counties.common.exports import (
    EXPORT_CSV_MAX_ROWS,
    has_meaningful_export_filter,
    render_protest_csv,
    render_protest_pdf,
    render_search_csv,
)


def consistent_published_read(view):
    """Keep all county queries in a shared response on one database snapshot."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        outer = not connection.in_atomic_block
        with transaction.atomic():
            if outer and connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            response = view(*args, **kwargs)
            if hasattr(response, "render"):
                response.render()
            return response

    return wrapped


RESULTS_PER_PAGE = 200

# Bounds for the comparables page's tuning controls.
SIMILAR_DEFAULT_MAX_DISTANCE = 10.0
SIMILAR_MIN_MAX_DISTANCE = 0.1
SIMILAR_MAX_MAX_DISTANCE = 50.0
SIMILAR_DEFAULT_MAX_RESULTS = 20
SIMILAR_MIN_MAX_RESULTS = 1
SIMILAR_MAX_MAX_RESULTS = 100
SIMILAR_DEFAULT_MIN_SCORE = 30.0
SIMILAR_MIN_MIN_SCORE = 0.0
SIMILAR_MAX_MIN_SCORE = 100.0


def clamped_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(upper, parsed))


def _form_params(adapter: CountyAdapter, source: Mapping[str, str]) -> dict[str, str]:
    params = {
        field.name: str(source.get(field.name, "")).strip()
        for field in adapter.profile.search_fields
    }
    params["sort"] = source.get("sort", _default_sort(adapter))
    params["dir"] = source.get("dir", "asc")
    return params


def _default_sort(adapter: CountyAdapter) -> str:
    for column in adapter.profile.search_columns:
        if column.sort_key:
            return column.sort_key
    return ""


def _county_url(adapter: CountyAdapter, view: str, *args) -> str:
    return reverse(adapter.profile.url_name(view), args=args)


# --------------------------------------------------------------------------- search


def index(request, *, adapter: CountyAdapter):
    """Property search: the county's own form fields over its own records."""
    profile = adapter.profile
    source = request.GET if request.method == "GET" else request.POST
    params = _form_params(adapter, source)

    filters_applied = any(params[field.name] for field in profile.search_fields)

    results: list[dict[str, Any]] = []
    page_obj = None
    if filters_applied:
        queryset = adapter.search_queryset(params)
        page_obj = Paginator(queryset, RESULTS_PER_PAGE).get_page(source.get("page", "1"))
        results = adapter.search_rows(list(page_obj.object_list))

    page_query = request.GET.copy()
    page_query.pop("page", None)
    sort_query_params = page_query.copy()
    sort_query_params.pop("sort", None)
    sort_query_params.pop("dir", None)

    context = {
        "county": profile,
        "results": results,
        "columns": profile.search_columns,
        "page_obj": page_obj,
        "base_query": page_query.urlencode(),
        "sort_query": sort_query_params.urlencode(),
        "form_values": params,
        "filters_applied": filters_applied,
        "sort": params["sort"],
        "dir": params["dir"],
        "export_url": (
            _county_url(adapter, "export_csv") if profile.supports_search_export else None
        ),
    }
    context.update(adapter.search_context(params))
    return render(request, "counties/index.html", context)


def export_csv(request, *, adapter: CountyAdapter):
    """Bulk CSV of the search results, gated behind a meaningful filter."""
    profile = adapter.profile
    if not profile.supports_search_export:
        raise Http404("This county does not support search exports.")

    params = _form_params(adapter, request.GET)
    if not has_meaningful_export_filter(profile, params):
        return HttpResponseBadRequest(
            "Export requires meaningful search criteria: a 5-digit ZIP code or at least "
            "3 non-space characters in a text filter."
        )

    queryset = adapter.search_queryset(params)
    records = list(queryset[:EXPORT_CSV_MAX_ROWS])
    rows = adapter.search_rows(records)
    return render_search_csv(profile.csv_columns, rows).to_response()


# --------------------------------------------------------------------------- comparables


def similar_properties(request, key, *, adapter: CountyAdapter):
    """Comparables ranked by similarity, with a protest recommendation."""
    profile = adapter.profile
    outcome = build_comparables_dossier(
        adapter,
        key,
        max_distance=request.GET.get("max_distance"),
        max_results=request.GET.get("max_results"),
        min_score=request.GET.get("min_score"),
    )
    if not outcome.is_ready:
        return render(
            request,
            "counties/similar_properties.html",
            {
                "county": profile,
                "subject": outcome.subject,
                "error": (
                    outcome.error
                    or "This property does not have location data required for similarity search."
                ),
                "subject_key": key,
            },
        )
    dossier = outcome.dossier
    assert dossier is not None

    context = {
        "county": profile,
        "subject": dossier.subject,
        "comps": dossier.comps,
        "columns": profile.comp_columns,
        "subject_percentile": dossier.subject_percentile,
        "assessment_history": dossier.history,
        "assessment_history_chart": dossier.assessment_history_chart,
        "recommendation": dossier.recommendation,
        "max_distance": dossier.max_distance,
        "max_results": dossier.max_results,
        "min_score": dossier.min_score,
        "search_url": _county_url(adapter, "index"),
        "protest_url": _county_url(adapter, "protest_analysis", dossier.subject.key),
    }
    return render(request, "counties/similar_properties.html", context)


# --------------------------------------------------------------------------- protest report


def protest_analysis(request, key, *, adapter: CountyAdapter):
    """ARB evidence report: equity comparison, tax impact, comparable table."""
    profile = adapter.profile
    outcome = build_protest_dossier(adapter, key, min_score=request.GET.get("min_score"))
    if not outcome.is_ready:
        if outcome.error == "Property not found":
            raise Http404("Property not found")
        subject = outcome.subject or adapter.get_subject(key)
        return render(
            request,
            "counties/protest_analysis.html",
            {
                "county": profile,
                "subject": subject,
                "error": outcome.error,
                "subject_key": key,
            },
        )
    dossier = outcome.dossier
    assert dossier is not None

    context = {
        "county": profile,
        "subject": dossier.subject,
        "comps": dossier.comps,
        "comp_rows": dossier.comp_rows,
        "columns": profile.comp_columns,
        "equity": dossier.equity,
        "assessment_history": dossier.history,
        "history_notice": dossier.history_notice,
        "assessment_history_chart": dossier.assessment_history_chart,
        "ppsf_distribution_chart": dossier.ppsf_distribution_chart,
        "tax_impact": dossier.tax_impact,
        "min_score": dossier.min_score,
        "min_score_floor": int(PROTEST_MIN_MIN_SCORE),
        "min_score_ceiling": int(PROTEST_MAX_MIN_SCORE),
        "back_url": _county_url(adapter, "similar_properties", dossier.subject.key),
        "back_label": "Back to Similar Properties",
        "export_url": _county_url(adapter, "protest_analysis_export", dossier.subject.key),
        "pdf_url": _county_url(adapter, "protest_analysis_pdf", dossier.subject.key),
    }
    return render(request, "counties/protest_analysis.html", context)


def protest_analysis_export(request, key, *, adapter: CountyAdapter):
    """CSV of the report's comparable table plus its tax-impact totals."""
    outcome = build_protest_dossier(adapter, key, min_score=request.GET.get("min_score"))
    if not outcome.is_ready:
        if outcome.error == "Property not found":
            raise Http404("Property not found")
        return HttpResponseBadRequest(
            outcome.error
            or "This property does not have location data required for similarity search."
        )
    dossier = outcome.dossier
    assert dossier is not None
    return render_protest_csv(dossier).to_response()


def protest_analysis_pdf(request, key, *, adapter: CountyAdapter):
    """Printable evidence report."""
    outcome = build_protest_dossier(adapter, key, min_score=request.GET.get("min_score"))
    if not outcome.is_ready:
        if outcome.error == "Property not found":
            raise Http404("Property not found")
        return HttpResponseBadRequest(
            outcome.error
            or "This property does not have location data required for similarity search."
        )
    dossier = outcome.dossier
    assert dossier is not None
    return render_protest_pdf(adapter.profile, dossier).to_response()


__all__ = [
    "Comp",
    "Subject",
    "clamped_float",
    "clamped_int",
    "consistent_published_read",
    "export_csv",
    "index",
    "protest_analysis",
    "protest_analysis_export",
    "protest_analysis_pdf",
    "similar_properties",
]
