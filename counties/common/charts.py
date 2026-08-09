"""Pure-data SVG layout for the two charts every county's report shows.

These return plain dicts of coordinates; the templates draw the SVG. Nothing
here touches a model, which is why both counties can share one copy.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from typing import Any


def score_breakdown_summary(components: Sequence[dict[str, Any]]) -> str:
    """One-line ``"Living area: 20.4/24; Bedrooms: 14.0/14"`` for CSV columns."""
    parts = []
    for component in components:
        if component.get("points") is None:
            continue
        parts.append(f"{component['label']}: {component['points']}/{component['weight']}")
    return "; ".join(parts)


def assessment_history_chart(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """Line chart of assessed value over time, oldest year on the left."""
    values = [
        (int(row["tax_year"]), float(row["assessed_value"]))
        for row in rows
        if row.get("assessed_value") is not None
    ]
    if not values:
        return None

    values.sort(key=lambda item: item[0])
    width = 520.0
    height = 180.0
    left = 40.0
    right = 16.0
    top = 16.0
    bottom = 28.0
    plot_width = width - left - right
    plot_height = height - top - bottom

    amounts = [amount for _, amount in values]
    min_amount = min(amounts)
    max_amount = max(amounts)
    amount_span = max(max_amount - min_amount, 1.0)
    year_span = max(len(values) - 1, 1)

    points = []
    for index, (year, amount) in enumerate(values):
        x = left + (plot_width * index / year_span)
        y = top + plot_height - (((amount - min_amount) / amount_span) * plot_height)
        points.append({"year": year, "amount": amount, "x": round(x, 2), "y": round(y, 2)})

    if len(points) == 1:
        path = f"M {points[0]['x']} {points[0]['y']}"
    else:
        path = "M " + " L ".join(f"{point['x']} {point['y']}" for point in points)

    y_ticks = []
    for idx in range(3):
        ratio = idx / 2
        y_ticks.append(
            {
                "amount": max_amount - (amount_span * ratio),
                "y": round(top + (plot_height * ratio), 2),
            }
        )

    return {
        "width": round(width),
        "height": round(height),
        "path": path,
        "points": points,
        "y_ticks": y_ticks,
        "baseline_y": round(top + plot_height, 2),
        "left": round(left, 2),
        "right": round(width - right, 2),
    }


def ppsf_distribution_chart(
    comp_values: Sequence[float], subject_value: float | None, bins: int = 10
) -> dict[str, Any] | None:
    """Histogram of comparable $/sqft with average and subject markers."""
    if not comp_values:
        return None

    values = sorted(comp_values)
    min_value = values[0]
    max_value = values[-1]
    if max_value == min_value:
        max_value = min_value + 1.0

    bin_count = max(4, min(12, bins))
    bin_size = (max_value - min_value) / bin_count
    counts = [0] * bin_count
    for value in values:
        index = int((value - min_value) / bin_size)
        counts[min(index, bin_count - 1)] += 1

    max_count = max(counts) if counts else 1
    bar_width = 32
    bar_gap = 6
    chart_height = 170
    chart_top = 20
    chart_bottom = 34
    axis_y = chart_top + chart_height

    bars: list[dict[str, Any]] = []
    for idx, count in enumerate(counts):
        height = (count / max_count) * chart_height if max_count else 0
        low = min_value + (idx * bin_size)
        bars.append(
            {
                "x": round(idx * (bar_width + bar_gap), 2),
                "y": round(axis_y - height, 2),
                "width": bar_width,
                "height": round(height, 2),
                "count": count,
                "low": round(low, 2),
                "high": round(low + bin_size, 2),
            }
        )

    width = (bin_count * bar_width) + ((bin_count - 1) * bar_gap)
    span = max_value - min_value

    average_value = statistics.mean(values)
    average_x = round(max(0.0, min(1.0, (average_value - min_value) / span)) * width, 2)

    subject_x = None
    if subject_value is not None:
        subject_x = round(max(0.0, min(1.0, (subject_value - min_value) / span)) * width, 2)

    x_ticks: list[dict[str, Any]] = []
    for idx in sorted({0, max(0, bin_count // 2), bin_count - 1}):
        bar = bars[idx]
        x_ticks.append(
            {
                "x": round(float(bar["x"]) + (bar_width / 2), 2),
                "label": f"${bar['low']:.0f}-${bar['high']:.0f}",
            }
        )

    return {
        "bars": bars,
        "width": width,
        "height": chart_top + chart_height + chart_bottom,
        "axis_y": axis_y,
        "min_value": round(min_value, 2),
        "max_value": round(max_value, 2),
        "average_value": round(average_value, 2),
        "average_x": average_x,
        "max_count": max_count,
        "subject_x": subject_x,
        "x_ticks": x_ticks,
    }
