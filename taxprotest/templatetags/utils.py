from django import template

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


@register.simple_tag
def sort_url(base_query: str, sort: str, current_sort: str, current_dir: str):
    """Build a sort URL toggling direction when the same column is clicked."""
    next_dir = "desc" if current_sort == sort and current_dir == "asc" else "asc"
    if base_query:
        return f"?{base_query}&sort={sort}&dir={next_dir}"
    return f"?sort={sort}&dir={next_dir}"
