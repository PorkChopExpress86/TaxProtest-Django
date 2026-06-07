from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from data.models import PropertyJurisdictionExemption

_COPY_NULL = r"\N"


def _to_decimal(value) -> Decimal | None:
    """Return a Decimal for numeric strings; None for blanks, 'Pending', etc."""
    if not value:
        return None
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        return None


def _copy_field(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _s(value: str | None, maxlen: int) -> str:
    if not value:
        return ""
    return _copy_field(value.strip()[:maxlen])


def _dec(value: str | None) -> str:
    if not value:
        return _COPY_NULL
    v = value.strip().replace("$", "").replace(",", "")
    if not v:
        return _COPY_NULL
    try:
        return repr(float(v))
    except (ValueError, TypeError):
        return _COPY_NULL


class _GeneratorIO(io.RawIOBase):
    """Adapt a str-line generator into a file-like object for copy_expert."""

    def __init__(self, gen):
        self._gen = gen
        self._buf = b""

    def readable(self):
        return True

    def readinto(self, buf):
        while not self._buf:
            try:
                self._buf = next(self._gen).encode()
            except StopIteration:
                return 0
        n = min(len(buf), len(self._buf))
        buf[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


class Command(BaseCommand):
    help = "Import account-level jurisdiction/exemption rows (Real_jur_exempt-derived)"

    def add_arguments(self, parser):
        parser.add_argument("--path", required=True)
        parser.add_argument("--tax-year", type=int, required=True)
        parser.add_argument("--delimiter", default="\t")
        parser.add_argument("--source", default="hcad_real_jur_exempt")

    def handle(self, *args, **options):
        file_path = Path(options["path"])
        if not file_path.exists():
            raise CommandError(f"File not found: {file_path}")

        tax_year = options["tax_year"]
        delimiter = options["delimiter"]
        source = options["source"]

        if connection.vendor == "postgresql":
            upserted = self._copy_load(file_path, tax_year, delimiter, source)
        else:
            upserted = self._orm_load(file_path, tax_year, delimiter, source)

        self.stdout.write(
            self.style.SUCCESS(f"Upserted {upserted} jurisdiction/exemption rows for {tax_year}.")
        )

    # ------------------------------------------------------------------
    # PostgreSQL fast path: DELETE-for-year + COPY
    # ------------------------------------------------------------------

    def _copy_load(self, file_path: Path, tax_year: int, delimiter: str, source: str) -> int:
        table = PropertyJurisdictionExemption._meta.db_table
        now_iso = timezone.now().isoformat()
        source_esc = _copy_field(source)
        loaded = 0

        fh = open(file_path, encoding="latin-1", errors="ignore", newline="")
        try:
            reader = csv.reader(fh, delimiter=delimiter)
            header = next(reader, None)
            if not header:
                raise CommandError("Input file has no header row")

            lower = {name.lower(): i for i, name in enumerate(header) if name}

            # Resolve column indices — support multiple HCAD naming conventions.
            acct_idx = next(
                (lower[k] for k in ("account_number", "acct", "account") if k in lower), None
            )
            unit_idx = next(
                (lower[k] for k in ("tax_unit_code", "tax_district", "tax_dist", "tax_unit", "unit_code") if k in lower),
                None,
            )
            cat_idx = next(
                (lower[k] for k in ("exemption_code", "exempt_cat", "exempt_code") if k in lower),
                None,
            )
            val_idx = next(
                (lower[k] for k in ("exemption_amount", "exempt_val", "exempt_amt") if k in lower),
                None,
            )

            if acct_idx is None or unit_idx is None:
                raise CommandError(
                    f"Required columns not found. Header: {header}"
                )

            def rows():
                nonlocal loaded
                for row in reader:
                    if len(row) <= max(
                        acct_idx,
                        unit_idx,
                        cat_idx if cat_idx is not None else 0,
                        val_idx if val_idx is not None else 0,
                    ):
                        continue
                    acct = row[acct_idx].strip()
                    unit = row[unit_idx].strip()
                    if not acct or not unit:
                        continue
                    cat = row[cat_idx].strip() if cat_idx is not None else ""
                    val = _dec(row[val_idx] if val_idx is not None else None)
                    year_str = str(tax_year)
                    fields = [
                        _copy_field(acct),
                        year_str,
                        _copy_field(unit[:32]),
                        "",           # tax_unit_name
                        _copy_field(cat[:32]),
                        "",           # exemption_description
                        val,          # exemption_amount (may be \N)
                        _COPY_NULL,   # exemption_percent
                        _COPY_NULL,   # taxable_value
                        _COPY_NULL,   # assessed_value
                        source_esc,
                        _copy_field(now_iso),
                        _copy_field(now_iso),
                    ]
                    loaded += 1
                    yield "\t".join(fields) + "\n"

            columns = (
                "account_number, tax_year, tax_unit_code, tax_unit_name, "
                "exemption_code, exemption_description, exemption_amount, "
                "exemption_percent, taxable_value, assessed_value, "
                "source, created_at, updated_at"
            )

            with transaction.atomic(), connection.cursor() as cursor:
                # COPY into a temp table (no unique constraint) so duplicate
                # rows in the source file don't abort the transaction.
                #
                # Drop any pre-existing staging table first. ON COMMIT DROP only
                # fires when the surrounding transaction commits; under Django's
                # TestCase (which wraps each test in an un-committed transaction)
                # a second invocation on the same connection would otherwise hit
                # "relation _jur_exempt_staging already exists".
                cursor.execute("DROP TABLE IF EXISTS _jur_exempt_staging")
                cursor.execute(
                    """
                    CREATE TEMP TABLE _jur_exempt_staging (
                        account_number VARCHAR(20),
                        tax_year INTEGER,
                        tax_unit_code VARCHAR(32),
                        tax_unit_name VARCHAR(255),
                        exemption_code VARCHAR(32),
                        exemption_description VARCHAR(255),
                        exemption_amount NUMERIC(14,2),
                        exemption_percent NUMERIC(8,4),
                        taxable_value NUMERIC(14,2),
                        assessed_value NUMERIC(14,2),
                        source VARCHAR(64),
                        created_at TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ
                    ) ON COMMIT DROP
                    """
                )
                cursor.copy_expert(
                    f'COPY _jur_exempt_staging ({columns}) FROM STDIN WITH (FORMAT text, NULL \'\\N\')',
                    _GeneratorIO(rows()),
                )
                cursor.execute(
                    f"DELETE FROM \"{table}\" WHERE tax_year = %s",
                    [tax_year],
                )
                cursor.execute(
                    f"""
                    INSERT INTO "{table}" ({columns})
                    SELECT DISTINCT ON (account_number, tax_year, tax_unit_code, exemption_code)
                        {columns}
                    FROM _jur_exempt_staging
                    ORDER BY account_number, tax_year, tax_unit_code, exemption_code
                    ON CONFLICT (account_number, tax_year, tax_unit_code, exemption_code)
                    DO UPDATE SET
                        exemption_amount = EXCLUDED.exemption_amount,
                        source = EXCLUDED.source,
                        updated_at = EXCLUDED.updated_at
                    """
                )
        finally:
            fh.close()

        return loaded

    # ------------------------------------------------------------------
    # Generic ORM fallback (non-PostgreSQL)
    # ------------------------------------------------------------------

    def _orm_load(self, file_path: Path, tax_year: int, delimiter: str, source: str) -> int:
        upserted = 0
        with file_path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
            reader = csv.DictReader(fh, delimiter=delimiter)
            if reader.fieldnames is None:
                raise CommandError("Input file has no header row")

            for row in reader:
                account_number = (
                    row.get("account_number") or row.get("acct") or row.get("account") or ""
                ).strip()
                tax_unit_code = (
                    row.get("tax_unit_code")
                    or row.get("tax_district")
                    or row.get("tax_dist")
                    or row.get("tax_unit")
                    or row.get("unit_code")
                    or ""
                ).strip()
                exemption_code = (
                    row.get("exemption_code")
                    or row.get("exempt_cat")
                    or row.get("exempt_code")
                    or ""
                ).strip()
                if not account_number or not tax_unit_code:
                    continue

                PropertyJurisdictionExemption.objects.update_or_create(
                    account_number=account_number,
                    tax_year=tax_year,
                    tax_unit_code=tax_unit_code,
                    exemption_code=exemption_code,
                    defaults={
                        "tax_unit_name": (
                            row.get("tax_unit_name") or row.get("tax_dist_name") or row.get("unit_name") or ""
                        ).strip(),
                        "exemption_description": (
                            row.get("exemption_description")
                            or row.get("exemption_dscr")
                            or row.get("exempt_dscr")
                            or row.get("exempt_desc")
                            or ""
                        ).strip(),
                        "exemption_amount": _to_decimal(
                            row.get("exemption_amount")
                            or row.get("exempt_val")
                            or row.get("exempt_amt")
                        ),
                        "exemption_percent": _to_decimal(
                            row.get("exemption_percent")
                            or row.get("pct_exempt")
                            or row.get("exempt_pct")
                        ),
                        "taxable_value": _to_decimal(
                            row.get("taxable_value")
                            or row.get("taxable_val")
                            or row.get("taxable")
                        ),
                        "assessed_value": _to_decimal(
                            row.get("assessed_value")
                            or row.get("appraised_val")
                            or row.get("assessed")
                        ),
                        "source": source,
                    },
                )
                upserted += 1

        return upserted
