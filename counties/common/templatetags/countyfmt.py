"""Formatting helpers shared by every county's property pages."""

from collections.abc import Mapping

from django import template

from counties.common.contracts import QUALITY_LABELS

register = template.Library()


@register.filter
def currency(value):
    try:
        if value is None:
            return ""
        return f"${float(value):,.0f}"
    except Exception:
        return ""


@register.filter
def sqft(value):
    try:
        if value is None:
            return ""
        return f"{float(value):,.0f}"
    except Exception:
        return ""


@register.filter
def acres(value):
    try:
        if value is None:
            return ""
        return f"{float(value):,.2f}"
    except Exception:
        return ""


@register.filter
def field(obj, name):
    """Read ``name`` off a row, whether the row is a mapping or an object.

    Search results arrive as dicts and comparables as dataclasses; the shared
    column-driven templates should not care which.
    """
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


@register.filter
def quality_label(code):
    """Expand a single-letter quality grade, e.g. ``"C"`` -> ``"Average"``."""
    if not code:
        return ""
    return QUALITY_LABELS.get(str(code).upper(), str(code))


@register.filter
def quality_classes(code):
    """Tailwind badge colours for a quality grade."""
    return {
        "X": "bg-purple-100 text-purple-800",
        "A": "bg-green-100 text-green-800",
        "B": "bg-blue-100 text-blue-800",
        "C": "bg-yellow-100 text-yellow-800",
        "D": "bg-orange-100 text-orange-800",
    }.get(str(code or "").upper(), "bg-gray-100 text-gray-800")


@register.filter
def score_classes(score):
    """Tailwind badge colours for a similarity score."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "bg-gray-100 text-gray-800"
    if value >= 84:
        return "bg-green-100 text-green-800"
    if value >= 70:
        return "bg-blue-100 text-blue-800"
    return "bg-yellow-100 text-yellow-800"


@register.simple_tag
def sort_url(base_query: str, sort: str, current_sort: str, current_dir: str):
    """Build a sort URL toggling direction when the same column is clicked."""
    next_dir = "desc" if current_sort == sort and current_dir == "asc" else "asc"
    if base_query:
        return f"?{base_query}&sort={sort}&dir={next_dir}"
    return f"?sort={sort}&dir={next_dir}"


@register.inclusion_tag("components/sort_header.html")
def sort_header(
    label: str, key: str, base_query: str, current_sort: str, current_dir: str, align: str = "left"
):
    """Render a sortable table header with direction arrow and active styling."""
    is_active = current_sort == key
    next_dir = "desc" if is_active and current_dir == "asc" else "asc"
    if base_query:
        url = f"?{base_query}&sort={key}&dir={next_dir}"
    else:
        url = f"?sort={key}&dir={next_dir}"
    arrow = ""
    if is_active:
        arrow = "▲" if current_dir == "asc" else "▼"
    return {
        "label": label,
        "url": url,
        "is_active": is_active,
        "arrow": arrow,
        "align": align,
    }
