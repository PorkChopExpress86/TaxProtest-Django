"""CSV and PDF rendering shared by every county's protest report."""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from django.http import HttpResponse

from counties.common.analysis import (
    ComparableShortfall,
    EquitySummary,
    ProtestCompRow,
    ProtestEvidenceDossier,
    history_availability_notice,
    protest_comp_rows,
)
from counties.common.contracts import Column, Comp, CountyProfile, Subject
from counties.common.tax_impact import TaxImpactResult

#: Leading characters a spreadsheet would evaluate as a formula.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

#: Minimum non-space characters in a text filter before a bulk export is allowed.
EXPORT_MIN_TEXT_FILTER_LENGTH = 3

#: Hard ceiling on rows in a search-results export.
EXPORT_CSV_MAX_ROWS = 1000


@dataclass(frozen=True)
class ExportDocument:
    """Neutral container for rendered reports and exports."""

    filename: str
    content_type: str
    payload: bytes

    def to_response(self) -> HttpResponse:
        response = HttpResponse(self.payload, content_type=self.content_type)
        safe = self.filename.replace('"', "").replace("\\", "")
        response["Content-Disposition"] = f'attachment; filename="{safe}"'
        return response


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


def render_search_csv(
    columns: Sequence[Column],
    rows: Sequence[Mapping[str, Any]],
    filename: str = "property_search.csv",
) -> ExportDocument:
    """Render search results table to an ExportDocument using the county's column set."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
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

    return ExportDocument(
        filename=filename,
        content_type="text/csv",
        payload=buffer.getvalue().encode("utf-8"),
    )


def search_results_csv(
    columns: Sequence[Column], rows: Sequence[Mapping[str, Any]]
) -> HttpResponse:
    """Export the search results table using the county's own column set."""
    return render_search_csv(columns, rows).to_response()


def _build_protest_csv_doc(
    subject: Subject,
    comp_rows: Sequence[ProtestCompRow],
    tax_impact: TaxImpactResult,
    history_warning: str = "",
    comparable_shortfall: ComparableShortfall | None = None,
) -> ExportDocument:
    buffer = io.StringIO()
    writer = csv.writer(buffer)

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
    header += [
        "tax_year_used",
        "tax_impact_completeness",
        "current_tax_owed",
        "median_tax_owed",
        "estimated_tax_savings",
        "tax_impact_warnings",
        "property_source_year",
        "assessment_history_availability",
        "comparable_shortfall",
    ]
    writer.writerow(header)
    shortfall_text = comparable_shortfall.message if comparable_shortfall else ""

    for entry in comp_rows:
        comp = entry.comp
        ppsf = entry.value_per_sqft
        delta = entry.delta
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
            entry.breakdown_summary,
        ]
        row += [
            tax_impact.tax_year or "",
            tax_impact.completeness,
            f"{float(tax_impact.current_tax_owed):.2f}" if tax_impact.may_show_totals else "",
            f"{float(tax_impact.median_tax_owed):.2f}" if tax_impact.may_show_totals else "",
            f"{float(tax_impact.estimated_savings):.2f}" if tax_impact.may_show_totals else "",
            " | ".join(tax_impact.warnings),
            subject.tax_year or "Not recorded",
            csv_safe_text(history_warning),
            shortfall_text,
        ]
        writer.writerow(row)
    if not comp_rows and comparable_shortfall:
        # One notice-only row, so a file with no comparables still states the shortfall.
        writer.writerow([""] * (len(header) - 1) + [shortfall_text])

    safe_key = str(subject.key).replace('"', "").replace("\\", "")
    return ExportDocument(
        filename=f"protest_analysis_{safe_key}.csv",
        content_type="text/csv",
        payload=buffer.getvalue().encode("utf-8"),
    )


def render_protest_csv(dossier: ProtestEvidenceDossier) -> ExportDocument:
    """Render a completed protest evidence dossier to a CSV ExportDocument."""
    return _build_protest_csv_doc(
        subject=dossier.subject,
        comp_rows=dossier.comp_rows,
        tax_impact=dossier.tax_impact,
        history_warning=dossier.history_notice,
        comparable_shortfall=dossier.comparable_shortfall,
    )


def protest_comps_csv(
    subject: Subject,
    comps: Sequence[Comp],
    equity: EquitySummary,
    tax_impact: TaxImpactResult,
    history_warning: str = "",
) -> HttpResponse:
    """One row per comparable, with the shared tax-impact columns appended."""
    return _build_protest_csv_doc(
        subject=subject,
        comp_rows=protest_comp_rows(comps, equity.subject_value_per_sqft),
        tax_impact=tax_impact,
        history_warning=history_warning,
    ).to_response()


# --------------------------------------------------------------------------- PDF


def _pdf_escape(text: Any) -> str:
    return str(text or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


PDF_LINES_PER_PAGE = 38

#: The most comparables the evidence PDF lists, so the report stays about one page. The
#: renderer owns this layout policy; when the dossier holds more, the PDF says so.
PDF_COMPARABLE_CAP = 10


def simple_pdf(lines: Sequence[str], lines_per_page: int = PDF_LINES_PER_PAGE) -> bytes:
    """A minimal paginated PDF of left-aligned Helvetica text.

    Hand-rolled rather than pulled from a rendering library: the evidence report
    is a flat list of lines, and this keeps the image free of a native toolchain.
    Automatically chunks lines into pages to prevent vertical boundary overflow.
    """
    raw_lines = list(lines)
    if not raw_lines:
        pages_lines = [[]]
    else:
        pages_lines = [
            raw_lines[i : i + lines_per_page] for i in range(0, len(raw_lines), lines_per_page)
        ]

    page_count = len(pages_lines)
    objects: list[bytes] = []

    # Object 1: Catalog
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")

    # Object 2: Pages tree (kids are objects 4, 6, 8, ...)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(page_count))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("ascii"))

    # Object 3: Helvetica font definition
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    # Objects for each page: Page object followed by Content stream object
    for i, page_lines in enumerate(pages_lines):
        content_obj_id = 4 + 2 * i + 1
        page_obj = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_obj_id} 0 R >>"
        ).encode("ascii")
        objects.append(page_obj)

        text_commands = ["BT", "/F1 12 Tf", "72 760 Td"]
        for line_index, line in enumerate(page_lines):
            if line_index:
                text_commands.append("0 -18 Td")
            text_commands.append(f"({_pdf_escape(line)}) Tj")
        text_commands.append("ET")
        stream = "\n".join(text_commands).encode("latin-1", errors="replace")

        stream_obj = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )
        objects.append(stream_obj)

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


def _build_protest_pdf_doc(
    profile: CountyProfile,
    subject: Subject,
    comp_rows: Sequence[ProtestCompRow],
    history_rows: Sequence[Mapping[str, Any]],
    tax_impact: TaxImpactResult,
    comparable_count: int,
    comparable_shortfall: ComparableShortfall | None = None,
) -> ExportDocument:
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
    lines.append(f"Property Source Year: {subject.tax_year or 'Not recorded'}")
    notice = history_availability_notice(history_rows, subject.tax_year)
    if notice:
        lines.append(notice)

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

    if comp_rows or comparable_shortfall:
        lines.append("")
        lines.append("Comparable Evidence")
        if comparable_shortfall:
            lines.extend([comparable_shortfall.headline, comparable_shortfall.guidance])
        if comparable_count > PDF_COMPARABLE_CAP:
            lines.append(
                f"Showing the {PDF_COMPARABLE_CAP} closest of {comparable_count} comparables"
            )
        for entry in comp_rows[:PDF_COMPARABLE_CAP]:
            comp = entry.comp
            ppsf = entry.value_per_sqft
            ppsf_text = f", ${ppsf:,.2f}/sqft" if ppsf is not None else ""
            lines.append(f"{comp.address}: score {float(comp.similarity_score):.1f}{ppsf_text}")

    lines.extend(
        [
            "",
            "Tax Impact (Estimated)",
            f"Tax Year Used: {tax_impact.tax_year or '-'} ({tax_impact.completeness})",
        ]
    )
    if tax_impact.may_show_totals:
        lines.extend(
            [
                f"Current Taxes Owed: ${float(tax_impact.current_tax_owed):,.2f}",
                f"Median-Scenario Taxes Owed: ${float(tax_impact.median_tax_owed):,.2f}",
                f"Estimated Annual Savings: ${float(tax_impact.estimated_savings):,.2f}",
            ]
        )
    else:
        lines.append("Tax totals unavailable until matching-year inputs are complete.")
    if tax_impact.warnings:
        lines.append(f"Warnings: {' | '.join(tax_impact.warnings)}")

    pdf_bytes = simple_pdf(lines)
    safe_key = str(subject.key).replace('"', "").replace("\\", "")
    return ExportDocument(
        filename=f"protest_analysis_{safe_key}.pdf",
        content_type="application/pdf",
        payload=pdf_bytes,
    )


def render_protest_pdf(
    profile: CountyProfile,
    dossier: ProtestEvidenceDossier,
) -> ExportDocument:
    """Render a completed protest evidence dossier to a PDF ExportDocument."""
    return _build_protest_pdf_doc(
        profile=profile,
        subject=dossier.subject,
        comp_rows=dossier.comp_rows,
        history_rows=dossier.history,
        tax_impact=dossier.tax_impact,
        comparable_count=dossier.comparable_count,
        comparable_shortfall=dossier.comparable_shortfall,
    )


def protest_report_pdf(
    profile: CountyProfile,
    subject: Subject,
    comps: Sequence[Comp],
    history_rows: Sequence[Mapping[str, Any]],
    tax_impact: TaxImpactResult,
) -> HttpResponse:
    """The printable evidence report: subject, history, comparables, tax impact."""
    comp_rows = protest_comp_rows(comps, subject.value_per_sqft)
    return _build_protest_pdf_doc(
        profile=profile,
        subject=subject,
        comp_rows=comp_rows,
        history_rows=history_rows,
        tax_impact=tax_impact,
        comparable_count=len(comp_rows),
    ).to_response()


def render_protest_export(
    profile: CountyProfile,
    dossier: ProtestEvidenceDossier,
    format: Literal["csv", "pdf"] = "csv",
) -> ExportDocument:
    """Unified protest export dispatcher returning an ExportDocument."""
    if format == "csv":
        return render_protest_csv(dossier)
    if format == "pdf":
        return render_protest_pdf(profile, dossier)
    raise ValueError(f"Unsupported export format: {format}")
