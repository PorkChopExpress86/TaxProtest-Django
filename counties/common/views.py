"""The three property pages, rendered identically for every county.

Each view takes a :class:`~counties.common.contracts.CountyAdapter`, asks it for
neutral records, and does all analysis and presentation here.
``county_urlpatterns`` in :mod:`counties.common.urls` binds an adapter to these.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.core.paginator import Paginator
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import render
from django.urls import reverse

from counties.common.analysis import (
    percentile_of,
    recommend_protest,
    summarize_equity,
)
from counties.common.charts import (
    assessment_history_chart,
    ppsf_distribution_chart,
    score_breakdown_summary,
)
from counties.common.contracts import Comp, CountyAdapter, Subject
from counties.common.exports import (
    EXPORT_CSV_MAX_ROWS,
    has_meaningful_export_filter,
    protest_comps_csv,
    protest_report_pdf,
    search_results_csv,
)

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

# The protest report only argues equity from reasonably close matches, so its
# floor is higher than the exploratory comparables page's.
PROTEST_DEFAULT_MIN_SCORE = 70.0
PROTEST_MIN_MIN_SCORE = 52.0
PROTEST_MAX_MIN_SCORE = 100.0
PROTEST_MAX_COMPS = 50
PROTEST_MAX_DISTANCE = 10.0


def clamped_float(value: Any, default: float, lower: float, upper: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(lower, min(upper, parsed))


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
        if queryset is not None:
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
    records = list(queryset[:EXPORT_CSV_MAX_ROWS]) if queryset is not None else []
    rows = adapter.search_rows(records)
    return search_results_csv(profile.csv_columns, rows)


# --------------------------------------------------------------------------- comparables


def _subject_or_404(adapter: CountyAdapter, key: str) -> Subject:
    subject = adapter.get_subject(key)
    if subject is None:
        raise Http404("Property not found")
    return subject


def _no_location_context(adapter: CountyAdapter, subject: Subject) -> dict[str, Any]:
    return {
        "county": adapter.profile,
        "subject": subject,
        "error": ("This property does not have location data required for similarity search."),
    }


def similar_properties(request, key, *, adapter: CountyAdapter):
    """Comparables ranked by similarity, with a protest recommendation."""
    profile = adapter.profile
    subject = adapter.get_subject(key)
    if subject is None:
        return render(
            request,
            "counties/similar_properties.html",
            {"county": profile, "error": "Property not found", "subject_key": key},
        )

    if not subject.has_location:
        return render(
            request, "counties/similar_properties.html", _no_location_context(adapter, subject)
        )

    max_distance = clamped_float(
        request.GET.get("max_distance"),
        SIMILAR_DEFAULT_MAX_DISTANCE,
        SIMILAR_MIN_MAX_DISTANCE,
        SIMILAR_MAX_MAX_DISTANCE,
    )
    max_results = clamped_int(
        request.GET.get("max_results"),
        SIMILAR_DEFAULT_MAX_RESULTS,
        SIMILAR_MIN_MAX_RESULTS,
        SIMILAR_MAX_MAX_RESULTS,
    )
    min_score = clamped_float(
        request.GET.get("min_score"),
        SIMILAR_DEFAULT_MIN_SCORE,
        SIMILAR_MIN_MIN_SCORE,
        SIMILAR_MAX_MIN_SCORE,
    )

    comps = adapter.find_comps(
        key,
        max_distance_miles=max_distance,
        max_results=max_results,
        min_score=min_score,
    )
    # Best match first, then nearest, then cheapest per sqft — a stable order
    # that reads the way a homeowner scans the table.
    comps.sort(
        key=lambda comp: (
            -float(comp.similarity_score or 0),
            float(comp.distance or 0),
            comp.value_per_sqft if comp.value_per_sqft is not None else float("inf"),
            comp.key or "",
        )
    )

    subject_ppsf = subject.value_per_sqft
    population = [c.value_per_sqft for c in comps if c.value_per_sqft is not None]
    if subject_ppsf is not None:
        population.append(subject_ppsf)

    history = adapter.assessment_history(key)
    recommendation = recommend_protest(subject_ppsf, comps)

    context = {
        "county": profile,
        "subject": subject,
        "comps": comps,
        "columns": profile.comp_columns,
        "subject_percentile": percentile_of(subject_ppsf, population),
        "assessment_history": history,
        "assessment_history_chart": assessment_history_chart(history),
        "recommendation": recommendation,
        "max_distance": max_distance,
        "max_results": max_results,
        "min_score": min_score,
        "search_url": _county_url(adapter, "index"),
        "protest_url": _county_url(adapter, "protest_analysis", subject.key),
    }
    return render(request, "counties/similar_properties.html", context)


# --------------------------------------------------------------------------- protest report


def _protest_inputs(request, key: str, adapter: CountyAdapter):
    """Shared setup for the report and both of its exports."""
    subject = _subject_or_404(adapter, key)
    min_score = clamped_float(
        request.GET.get("min_score"),
        PROTEST_DEFAULT_MIN_SCORE,
        PROTEST_MIN_MIN_SCORE,
        PROTEST_MAX_MIN_SCORE,
    )
    comps = adapter.find_comps(
        key,
        max_distance_miles=PROTEST_MAX_DISTANCE,
        max_results=PROTEST_MAX_COMPS,
        min_score=min_score,
    )
    equity = summarize_equity(subject, comps)
    history = adapter.assessment_history(key)
    tax_year = history[0]["tax_year"] if history else subject.tax_year
    tax_impact = adapter.tax_impact(key, tax_year, equity.median_assessed_value)
    return subject, comps, equity, history, tax_impact, min_score


def protest_analysis(request, key, *, adapter: CountyAdapter):
    """ARB evidence report: equity comparison, tax impact, comparable table."""
    profile = adapter.profile
    subject = _subject_or_404(adapter, key)
    if not subject.has_location:
        return render(
            request, "counties/protest_analysis.html", _no_location_context(adapter, subject)
        )

    subject, comps, equity, history, tax_impact, min_score = _protest_inputs(request, key, adapter)

    comp_rows = [
        {
            "comp": comp,
            "value_per_sqft": comp.value_per_sqft,
            "delta": comp.delta_vs(equity.subject_value_per_sqft),
            "breakdown_summary": score_breakdown_summary(comp.score_breakdown),
        }
        for comp in comps
    ]

    context = {
        "county": profile,
        "subject": subject,
        "comps": comps,
        "comp_rows": comp_rows,
        "columns": profile.comp_columns,
        "equity": equity,
        "assessment_history": history,
        "assessment_history_chart": assessment_history_chart(history),
        "ppsf_distribution_chart": ppsf_distribution_chart(
            equity.qualifying_ppsf, equity.subject_value_per_sqft
        ),
        "tax_impact": tax_impact,
        "min_score": min_score,
        "min_score_floor": int(PROTEST_MIN_MIN_SCORE),
        "min_score_ceiling": int(PROTEST_MAX_MIN_SCORE),
        "back_url": _county_url(adapter, "similar_properties", subject.key),
        "back_label": "Back to Similar Properties",
        "export_url": _county_url(adapter, "protest_analysis_export", subject.key),
        "pdf_url": _county_url(adapter, "protest_analysis_pdf", subject.key),
    }
    return render(request, "counties/protest_analysis.html", context)


def protest_analysis_export(request, key, *, adapter: CountyAdapter):
    """CSV of the report's comparable table plus its tax-impact totals."""
    subject, comps, equity, _history, tax_impact, _min_score = _protest_inputs(
        request, key, adapter
    )
    return protest_comps_csv(subject, comps, equity, tax_impact)


def protest_analysis_pdf(request, key, *, adapter: CountyAdapter):
    """Printable evidence report."""
    subject, comps, _equity, history, tax_impact, _min_score = _protest_inputs(
        request, key, adapter
    )
    return protest_report_pdf(adapter.profile, subject, comps, history, tax_impact)


__all__ = [
    "Comp",
    "Subject",
    "clamped_float",
    "clamped_int",
    "export_csv",
    "index",
    "protest_analysis",
    "protest_analysis_export",
    "protest_analysis_pdf",
    "similar_properties",
]
