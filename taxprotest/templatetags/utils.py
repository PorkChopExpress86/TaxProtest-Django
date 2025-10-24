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


@register.inclusion_tag("components/sort_header.html")
def sort_header(label: str, key: str, base_query: str, current_sort: str, current_dir: str, align: str = "left"):
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
