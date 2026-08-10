"""CSV and PDF rendering shared by every county's protest report."""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from typing import Any

from django.http import HttpResponse

from counties.common.analysis import EquitySummary
from counties.common.charts import score_breakdown_summary
from counties.common.contracts import Column, Comp, CountyProfile, Subject

#: Leading characters a spreadsheet would evaluate as a formula.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

#: Minimum non-space characters in a text filter before a bulk export is allowed.
EXPORT_MIN_TEXT_FILTER_LENGTH = 3

#: Hard ceiling on rows in a search-results export.
EXPORT_CSV_MAX_ROWS = 1000


def csv_safe_text(value: Any) -> str:
    """Neutralise spreadsheet formula injection in exported free text."""
    text = str(value or "")
    if text.startswith(CSV_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def has_meaningful_export_filter(profile: CountyProfile, params: Mapping[str, str]) -> bool:
    """Guard bulk exports behind a filter narrow enough to be a real search."""
    if profile.export_zip_filter:
        zip_code = str(params.get(profile.export_zip_filter, "")).strip()
        if len(zip_code) == 5 and zip_code.isdigit():
            return True

    for name in profile.export_text_filters:
        value = params.get(name, "")
        if len("".join(str(value).split())) >= EXPORT_MIN_TEXT_FILTER_LENGTH:
            return True

    return False


def _attachment(filename: str) -> HttpResponse:
    response = HttpResponse(content_type="text/csv")
    safe = filename.replace('"', "").replace("\\", "")
    response["Content-Disposition"] = f'attachment; filename="{safe}"'
    return response


def search_results_csv(
    columns: Sequence[Column], rows: Sequence[Mapping[str, Any]]
) -> HttpResponse:
    """Export the search results table using the county's own column set."""
    response = _attachment("property_search.csv")
    writer = csv.writer(response)
    writer.writerow([column.label for column in columns])

    numeric_formats = {"currency", "sqft", "acres", "ppsf", "number"}
    for row in rows:
        record = []
        for column in columns:
            value = row.get(column.key)
            if value is None or value == "":
                record.append("")
            elif column.format in numeric_formats:
                record.append(value)
            else:
                record.append(csv_safe_text(value))
        writer.writerow(record)

    return response


def protest_comps_csv(
    subject: Subject,
    comps: Sequence[Comp],
    equity: EquitySummary,
    tax_impact: Any,
) -> HttpResponse:
    """One row per comparable, with the shared tax-impact columns appended."""
    response = _attachment(f"protest_analysis_{subject.key}.csv")
    writer = csv.writer(response)

    header = [
        "address",
        "similarity_score",
        "similarity_label",
        "living_area_sqft",
        "bedrooms",
        "bathrooms",
        "year_built",
        "quality_code",
        "condition_code",
        "assessed_value",
        "value_per_sqft",
        "delta_vs_subject_per_sqft",
        "score_breakdown",
    ]
    if tax_impact is not None:
        header += [
            "tax_year_used",
            "tax_impact_completeness",
            "current_tax_owed",
            "median_tax_owed",
            "estimated_tax_savings",
            "tax_impact_warnings",
        ]
    writer.writerow(header)

    subject_ppsf = equity.subject_value_per_sqft
    for comp in comps:
        ppsf = comp.value_per_sqft
        delta = comp.delta_vs(subject_ppsf)
        row = [
            csv_safe_text(comp.address),
            f"{comp.similarity_score:.1f}",
            comp.match_label,
            f"{comp.living_area:.0f}" if comp.living_area else "",
            comp.bedrooms if comp.bedrooms is not None else "",
            f"{float(comp.bathrooms):.1f}" if comp.bathrooms else "",
            comp.year_built or "",
            csv_safe_text(comp.quality_code) if comp.quality_code else "",
            csv_safe_text(comp.condition_code) if comp.condition_code else "",
            f"{float(comp.assessed_value):.2f}" if comp.assessed_value else "",
            f"{ppsf:.2f}" if ppsf is not None else "",
            f"{delta:.2f}" if delta is not None else "",
            score_breakdown_summary(comp.score_breakdown),
        ]
        if tax_impact is not None:
            row += [
                tax_impact.tax_year or "",
                tax_impact.completeness,
                f"{float(tax_impact.current_tax_owed):.2f}",
                f"{float(tax_impact.median_tax_owed):.2f}",
                f"{float(tax_impact.estimated_savings):.2f}",
                " | ".join(tax_impact.warnings),
            ]
        writer.writerow(row)

    return response


# --------------------------------------------------------------------------- PDF


def _pdf_escape(text: Any) -> str:
    return str(text or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def simple_pdf(lines: Sequence[str]) -> bytes:
    """A minimal single-page PDF of left-aligned Helvetica text.

    Hand-rolled rather than pulled from a rendering library: the evidence report
    is a flat list of lines, and this keeps the image free of a native toolchain.
    """
    text_commands = ["BT", "/F1 12 Tf", "72 760 Td"]
    for index, line in enumerate(lines):
        if index:
            text_commands.append("0 -18 Td")
        text_commands.append(f"({_pdf_escape(line)}) Tj")
    text_commands.append("ET")
    stream = "\n".join(text_commands).encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(payload)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def protest_report_pdf(
    profile: CountyProfile,
    subject: Subject,
    comps: Sequence[Comp],
    history_rows: Sequence[Mapping[str, Any]],
    tax_impact: Any,
    max_comps: int = 10,
) -> HttpResponse:
    """The printable evidence report: subject, history, comparables, tax impact."""
    assessed = subject.assessed_value
    lines = [
        f"{profile.display_name} Property Tax Protest Evidence Report",
        f"{profile.key_label}: {subject.key}",
        f"Property: {subject.address_line}",
        f"Assessed Value: ${float(assessed):,.0f}" if assessed else "Assessed Value: unavailable",
    ]
    if subject.living_area:
        lines.append(f"Living Area: {float(subject.living_area):,.0f} sqft")
        if subject.value_per_sqft is not None:
            lines.append(f"Subject Value/Sqft: ${subject.value_per_sqft:,.2f}")

    if history_rows:
        lines.append("")
        lines.append("Assessment History")
        for row in history_rows:
            assessed_text = (
                f"${float(row['assessed_value']):,.0f}" if row.get("assessed_value") else "-"
            )
            change_text = (
                f"{row['increase_percent']}%" if row.get("increase_percent") is not None else "-"
            )
            cap_status = row["cap_status"]["label"] if row.get("cap_status") else "Needs review"
            lines.append(f"{row['tax_year']}: {assessed_text}, YoY {change_text}, {cap_status}")

    if comps:
        lines.append("")
        lines.append("Comparable Evidence")
        for comp in comps[:max_comps]:
            ppsf = comp.value_per_sqft
            ppsf_text = f", ${ppsf:,.2f}/sqft" if ppsf is not None else ""
            lines.append(f"{comp.address}: score {float(comp.similarity_score):.1f}{ppsf_text}")

    if tax_impact is not None:
        lines.extend(
            [
                "",
                "Tax Impact (Estimated)",
                f"Tax Year Used: {tax_impact.tax_year or '-'} ({tax_impact.completeness})",
                f"Current Taxes Owed: ${float(tax_impact.current_tax_owed):,.2f}",
                f"Median-Scenario Taxes Owed: ${float(tax_impact.median_tax_owed):,.2f}",
                f"Estimated Annual Savings: ${float(tax_impact.estimated_savings):,.2f}",
            ]
        )
        if tax_impact.warnings:
            lines.append(f"Warnings: {' | '.join(tax_impact.warnings)}")

    response = HttpResponse(simple_pdf(lines), content_type="application/pdf")
    safe_key = str(subject.key).replace('"', "").replace("\\", "")
    response["Content-Disposition"] = f'attachment; filename="protest_analysis_{safe_key}.pdf"'
    return response
