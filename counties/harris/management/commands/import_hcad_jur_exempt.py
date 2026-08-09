"""Load Harris County jurisdiction, exemption, and tax-rate data from HCAD's
``Real_jur_exempt.zip`` extract.

This is what makes the protest report's Tax Impact section work for Harris:
``calculate_tax_impact`` needs a ``PropertyJurisdictionExemption`` row per
(account, taxing unit) plus a ``TaxUnitRate`` per unit, and nothing populated
them before. The Brazos equivalent is ``load_brazos_cad``'s
``_load_entity_info``; this command is the same shape against HCAD's files.

Source files (all tab-delimited with a header row, latin-1):

  jur_value.txt                       acct, tax_district, tp_cd, pct_district,
                                      appraised_val, taxable_val
  jur_exempt.txt                      acct, tax_district, exempt_cat, exempt_val
  jur_exemption_dscr.txt              exempt_cat, exemption_dscr
  jur_tax_dist_exempt_value_rate.txt  RP_TYPE, tax_dist, name, exempt_cd, prop,
                                      curr, exempt_val, exempt_rate

Two things about this data are easy to get wrong, and both are load-bearing:

1. **Rates are quoted per $100 of value.** HOUSTON ISD is published as
   ``0.878300``. ``calculate_tax_impact`` multiplies a taxable value by
   ``adopted_rate`` directly, so the rate is stored fractional (0.00878300),
   matching how Brazos stores its scraped rates. Skipping the divide overstates
   every tax figure by 100x.

2. **``taxable_val`` is already net of exemptions, and it is authoritative.**
   Storing that net figure as ``taxable_value`` would make
   ``calculate_tax_impact`` subtract the exemptions a *second* time, so the base
   row stores the **gross** appraised value and a companion row carries the
   reduction.

   That reduction is **derived** as ``appraised_val - taxable_val`` rather than
   summed from ``jur_exempt.txt``, because the two files disagree on about
   0.26% of (account, district) pairs in the real 2025 export, in both
   directions: ~19k pairs itemise a homestead that ``taxable_val`` does not
   reflect, and ~7k pairs show a reduction that is never itemised. Summing the
   itemised amounts would therefore report a current tax bill that contradicts
   HCAD's own. Deriving it instead makes ``calculate_tax_impact`` reproduce
   ``taxable_val`` exactly, by construction -- while still applying the same
   reduction to the median-comparable scenario, which is what keeps the
   estimated-savings figure an apples-to-apples comparison on a homestead.

   ``jur_exempt.txt`` is still read, for the exemption *codes and descriptions*
   attached to the derived row. The size of the disagreement is reported on
   every run (``_itemisation_gaps``), and the stored result is verified against
   ``taxable_val`` after insert (``_verify_applied``).

Row shape written per (account, taxing unit):

  * one **base** row, ``exemption_code=""``, carrying the gross appraised value
    in both ``assessed_value`` and ``taxable_value``. This is the row
    ``_dedupe_units`` picks as the unit's basis (empty code sorts first).
  * where a reduction was applied, one **offset** row carrying it as
    ``exemption_amount``, coded with whatever ``jur_exempt.txt`` itemises for
    that pair (``RES``, ``HIS+RES``, ...) or ``EXEMPT`` when it itemises
    nothing.

Usage:
    python manage.py import_hcad_jur_exempt --tax-year 2025
    python manage.py import_hcad_jur_exempt --tax-year 2025 --all-accounts
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from counties.common.tax_models import TaxUnitRate
from counties.harris.models import PropertyRecord

logger = logging.getLogger(__name__)

COUNTY = "harris"
SOURCE = "hcad_real_jur_exempt"

VALUE_FILE = "jur_value.txt"
EXEMPT_FILE = "jur_exempt.txt"
DESCRIPTION_FILE = "jur_exemption_dscr.txt"
RATE_FILE = "jur_tax_dist_exempt_value_rate.txt"

#: HCAD quotes tax rates per $100 of assessed value.
RATE_DIVISOR = Decimal("100")

#: Placeholder categories that mean "no exemption", not a real grant.
NON_EXEMPTION_CATEGORIES = {"", "NONE"}

#: Columns COPY'd into the staging table, in order.
STAGE_COLUMNS = (
    "account_number",
    "tax_year",
    "county",
    "tax_unit_code",
    "tax_unit_name",
    "exemption_code",
    "exemption_description",
    "exemption_amount",
    "taxable_value",
    "assessed_value",
    "source",
    "hcad_taxable",
)

COPY_CHUNK_ROWS = 250_000


def _clean(value: str | None) -> str:
    """Strip, and neutralise anything that would corrupt a COPY stream or a query.

    Beyond the characters COPY's text format treats specially, the real export
    contains a handful of taxing-district names that are a bare NUL byte, which
    PostgreSQL rejects in any string literal.
    """
    text = value or ""
    text = "".join(" " if ch == "\\" or ord(ch) < 32 or ord(ch) == 127 else ch for ch in text)
    return text.strip()


def _decimal_or_none(value: str | None) -> Decimal | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _read_rows(path: Path) -> Iterator[dict[str, str]]:
    # latin-1: HCAD exports are Windows-codepage text, and a stray high byte in
    # an owner/jurisdiction name must not abort a 14-million-row pass.
    with path.open("r", encoding="latin-1", newline="") as fh:
        yield from csv.DictReader(fh, delimiter="\t")


class Command(BaseCommand):
    help = "Import Harris jurisdiction/exemption rows and tax unit rates from Real_jur_exempt."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tax-year",
            type=int,
            required=True,
            help="Tax year these files describe (they carry no year of their own).",
        )
        parser.add_argument(
            "--path",
            help="Directory holding the extracted jur_*.txt files "
            "(default: <HCAD_EXTRACT_DIR>/Real_jur_exempt).",
        )
        parser.add_argument(
            "--rate-column",
            default="curr",
            choices=["curr", "prop"],
            help="Which published rate to store: curr (adopted, default) or prop (proposed).",
        )
        parser.add_argument(
            "--all-accounts",
            action="store_true",
            help="Load every account in the files, not just accounts already ingested "
            "as PropertyRecords (the files cover ~1.6M accounts including commercial).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse, stage, and reconcile without writing to the real tables.",
        )

    # ------------------------------------------------------------------ setup

    def _resolve_dir(self, option: str | None) -> Path:
        if option:
            directory = Path(option)
        else:
            directory = Path(settings.HCAD_EXTRACT_DIR) / "Real_jur_exempt"
        if not directory.is_dir():
            raise CommandError(
                f"Extract directory not found: {directory}. "
                "Download and extract Real_jur_exempt.zip first "
                "(python manage.py download_hcad, or the ETL pipeline's 'Real Jur Exempt' source)."
            )
        missing = [n for n in (VALUE_FILE, EXEMPT_FILE, RATE_FILE) if not (directory / n).exists()]
        if missing:
            raise CommandError(f"Missing required file(s) under {directory}: {', '.join(missing)}")
        return directory

    def _load_rates(
        self, directory: Path, tax_year: int, rate_column: str, *, dry_run: bool
    ) -> dict[str, str]:
        """Upsert TaxUnitRate rows; return the tax_district -> name map.

        The rate file has one row per (district, exemption code), so the rate
        and name repeat; collapse to one row per district.
        """
        names: dict[str, str] = {}
        rates: dict[str, Decimal] = {}

        for row in _read_rows(directory / RATE_FILE):
            if _clean(row.get("RP_TYPE")).lower() != "real":
                continue
            code = _clean(row.get("tax_dist"))
            if not code:
                continue
            names.setdefault(code, _clean(row.get("name")))
            if code in rates:
                continue
            published = _decimal_or_none(row.get(rate_column))
            if published is not None:
                # Published per $100; calculate_tax_impact multiplies directly.
                rates[code] = published / RATE_DIVISOR

        self.stdout.write(
            f"  {RATE_FILE}: {len(names):,} real taxing districts, {len(rates):,} with a rate"
        )
        if dry_run:
            return names

        with transaction.atomic():
            TaxUnitRate.objects.filter(tax_year=tax_year, county=COUNTY).delete()
            TaxUnitRate.objects.bulk_create(
                [
                    TaxUnitRate(
                        tax_year=tax_year,
                        county=COUNTY,
                        tax_unit_code=code,
                        tax_unit_name=names.get(code, ""),
                        adopted_rate=rate,
                        source=f"{SOURCE}:{rate_column}",
                    )
                    for code, rate in sorted(rates.items())
                ],
                batch_size=1000,
            )
        return names

    def _load_descriptions(self, directory: Path) -> dict[str, str]:
        path = directory / DESCRIPTION_FILE
        if not path.exists():
            logger.warning("%s missing; exemption descriptions will be blank", DESCRIPTION_FILE)
            return {}
        return {
            _clean(row.get("exempt_cat")): _clean(row.get("exemption_dscr"))
            for row in _read_rows(path)
        }

    def _account_filter(self, *, all_accounts: bool) -> set[str] | None:
        if all_accounts:
            return None
        accounts = set(PropertyRecord.objects.values_list("account_number", flat=True))
        if not accounts:
            raise CommandError(
                "No PropertyRecord rows exist to scope the import to. Import property "
                "records first, or pass --all-accounts."
            )
        self.stdout.write(f"  scoping to {len(accounts):,} already-ingested accounts")
        return accounts

    # ------------------------------------------------------------------ row streams

    def _base_rows(
        self, directory: Path, tax_year: int, names: dict[str, str], accounts: set[str] | None
    ) -> Iterator[tuple]:
        """One row per (account, taxing unit), carrying the GROSS appraised value.

        ``hcad_taxable`` rides along only so the staged data can be reconciled;
        it is not copied into the real table.
        """
        for row in _read_rows(directory / VALUE_FILE):
            account = _clean(row.get("acct"))
            unit = _clean(row.get("tax_district"))
            if not account or not unit:
                continue
            if accounts is not None and account not in accounts:
                continue

            appraised = _decimal_or_none(row.get("appraised_val"))
            net_taxable = _decimal_or_none(row.get("taxable_val"))
            yield (
                account,
                tax_year,
                COUNTY,
                unit,
                names.get(unit, ""),
                "",  # base row: empty exemption code sorts first for _dedupe_units
                "",
                None,
                appraised,  # taxable_value: gross, exemptions subtracted downstream
                appraised,
                SOURCE,
                net_taxable,
            )

    def _exemption_rows(
        self,
        directory: Path,
        tax_year: int,
        names: dict[str, str],
        descriptions: dict[str, str],
        accounts: set[str] | None,
    ) -> Iterator[tuple]:
        for row in _read_rows(directory / EXEMPT_FILE):
            category = _clean(row.get("exempt_cat")).upper()
            if category in NON_EXEMPTION_CATEGORIES:
                continue
            amount = _decimal_or_none(row.get("exempt_val"))
            if amount is None or amount == 0:
                continue

            account = _clean(row.get("acct"))
            unit = _clean(row.get("tax_district"))
            if not account or not unit:
                continue
            if accounts is not None and account not in accounts:
                continue

            yield (
                account,
                tax_year,
                COUNTY,
                unit,
                names.get(unit, ""),
                category,
                descriptions.get(category, ""),
                amount,
                None,
                None,
                SOURCE,
                None,
            )

    # ------------------------------------------------------------------ staging

    @staticmethod
    def _encode(value) -> str:
        return "\\N" if value is None else str(value)

    def _copy_into_stage(self, cursor, rows: Iterator[tuple]) -> int:
        """Stream rows into the staging table in bounded chunks."""
        total = 0
        buffer = StringIO()
        pending = 0

        def flush():
            nonlocal pending
            if not pending:
                return
            buffer.seek(0)
            cursor.cursor.copy_from(buffer, "jur_exempt_stage", columns=STAGE_COLUMNS)
            buffer.seek(0)
            buffer.truncate(0)
            pending = 0

        for row in rows:
            buffer.write("\t".join(self._encode(v) for v in row) + "\n")
            pending += 1
            total += 1
            if pending >= COPY_CHUNK_ROWS:
                flush()
        flush()
        return total

    def _itemisation_gaps(self, cursor) -> tuple[int, int, int]:
        """Measure how far ``jur_exempt.txt`` diverges from what was applied.

        HCAD's two files disagree about exemptions on a small fraction of
        properties, in both directions: an exemption itemised in
        ``jur_exempt.txt`` that ``taxable_val`` does not reflect, and a
        reduction in ``taxable_val`` that ``jur_exempt.txt`` never itemises.
        Neither is an error on our side and neither affects the stored figures
        (the applied offset is derived from ``taxable_val``, which is
        authoritative) -- but a sharp change in these counts between years is
        worth noticing, so they are reported rather than swallowed.

        Returns (pairs_checked, itemised_but_not_applied, applied_but_not_itemised).
        """
        cursor.execute("""
            WITH deduped AS (
                SELECT DISTINCT ON (account_number, tax_unit_code, exemption_code)
                    account_number, tax_unit_code, exemption_code,
                    taxable_value, hcad_taxable, exemption_amount
                FROM jur_exempt_stage
                ORDER BY account_number, tax_unit_code, exemption_code
            ),
            per_unit AS (
                SELECT
                    max(taxable_value) FILTER (WHERE exemption_code = '')  AS gross,
                    max(hcad_taxable)  FILTER (WHERE exemption_code = '')  AS hcad_net,
                    coalesce(
                        sum(exemption_amount) FILTER (WHERE exemption_code <> ''), 0
                    ) AS itemised
                FROM deduped
                GROUP BY account_number, tax_unit_code
            )
            SELECT
                count(*),
                count(*) FILTER (WHERE itemised > gross - hcad_net),
                count(*) FILTER (WHERE itemised < gross - hcad_net)
            FROM per_unit
            WHERE hcad_net IS NOT NULL AND gross IS NOT NULL
            """)
        return cursor.fetchone()

    def _verify_applied(self, cursor, tax_year: int) -> int:
        """Post-insert check: stored gross minus stored exemptions == HCAD's net.

        True by construction, so a non-zero result means a bug in the INSERT,
        not a quirk of the source data.
        """
        cursor.execute(
            """
            WITH stored AS (
                SELECT
                    p.account_number,
                    p.tax_unit_code,
                    max(p.taxable_value) FILTER (WHERE p.exemption_code = '') AS gross,
                    coalesce(
                        sum(p.exemption_amount) FILTER (WHERE p.exemption_code <> ''), 0
                    ) AS applied
                FROM data_propertyjurisdictionexemption p
                WHERE p.tax_year = %s AND p.county = %s
                GROUP BY p.account_number, p.tax_unit_code
            )
            SELECT count(*)
            FROM stored s
            JOIN (
                SELECT DISTINCT ON (account_number, tax_unit_code)
                    account_number, tax_unit_code, hcad_taxable
                FROM jur_exempt_stage
                WHERE exemption_code = ''
                ORDER BY account_number, tax_unit_code
            ) h USING (account_number, tax_unit_code)
            WHERE h.hcad_taxable IS NOT NULL
              AND greatest(s.gross - s.applied, 0) IS DISTINCT FROM h.hcad_taxable
            """,
            [tax_year, COUNTY],
        )
        return cursor.fetchone()[0]

    # ------------------------------------------------------------------ main

    def handle(self, *args, **options):
        tax_year: int = options["tax_year"]
        dry_run: bool = options["dry_run"]

        if connection.vendor != "postgresql":
            raise CommandError(
                "This command streams ~17 million rows via PostgreSQL COPY and requires "
                f"a PostgreSQL database (current vendor: {connection.vendor})."
            )

        directory = self._resolve_dir(options.get("path"))
        self.stdout.write(f"Reading HCAD jurisdiction data from {directory}")

        stamp = datetime.now(UTC)

        # Rates and exemption rows are written in one transaction: a failed
        # verification must not leave the rates behind without the rows they
        # apply to, which would silently change every account's tax figures.
        with transaction.atomic():
            names = self._load_rates(directory, tax_year, options["rate_column"], dry_run=dry_run)
            descriptions = self._load_descriptions(directory)
            self.stdout.write(f"  {DESCRIPTION_FILE}: {len(descriptions)} exemption codes")
            accounts = self._account_filter(all_accounts=options["all_accounts"])

            with connection.cursor() as cursor:
                # Dropped explicitly as well as ON COMMIT: under a TestCase the
                # surrounding atomic() is a savepoint, so the commit that would
                # clean this up never arrives between calls.
                cursor.execute("DROP TABLE IF EXISTS jur_exempt_stage")
                cursor.execute("""
                    CREATE TEMP TABLE jur_exempt_stage (
                        account_number        VARCHAR(20),
                        tax_year              INTEGER,
                        county                VARCHAR(16),
                        tax_unit_code         VARCHAR(32),
                        tax_unit_name         VARCHAR(255),
                        exemption_code        VARCHAR(32),
                        exemption_description VARCHAR(255),
                        exemption_amount      NUMERIC(14, 2),
                        taxable_value         NUMERIC(14, 2),
                        assessed_value        NUMERIC(14, 2),
                        source                VARCHAR(64),
                        hcad_taxable          NUMERIC(14, 2)
                    ) ON COMMIT DROP
                    """)

                base_count = self._copy_into_stage(
                    cursor, self._base_rows(directory, tax_year, names, accounts)
                )
                self.stdout.write(f"  {VALUE_FILE}: staged {base_count:,} base rows")

                exempt_count = self._copy_into_stage(
                    cursor,
                    self._exemption_rows(directory, tax_year, names, descriptions, accounts),
                )
                self.stdout.write(f"  {EXEMPT_FILE}: staged {exempt_count:,} exemption rows")

                pairs, over_itemised, under_itemised = self._itemisation_gaps(cursor)
                self.stdout.write(
                    f"  {pairs:,} (account, taxing unit) pairs; jur_exempt.txt itemises more "
                    f"than was applied on {over_itemised:,} and less on {under_itemised:,}"
                )

                if dry_run:
                    self.stdout.write(self.style.WARNING("[dry-run] nothing written"))
                    transaction.set_rollback(True)
                    return

                cursor.execute(
                    "DELETE FROM data_propertyjurisdictionexemption "
                    "WHERE tax_year = %s AND county = %s",
                    [tax_year, COUNTY],
                )
                deleted = cursor.rowcount

                # Two rows per (account, taxing unit):
                #
                #   base       exemption_code='', taxable_value = gross appraised
                #   applied    exemption_amount = gross - HCAD's taxable_val
                #
                # The offset is derived from taxable_val rather than summed from
                # jur_exempt.txt because the two disagree on ~0.26% of pairs and
                # taxable_val is the authoritative figure (see _itemisation_gaps).
                # Deriving it also makes calculate_tax_impact reproduce HCAD's
                # net exactly, by construction, while still applying the same
                # reduction to the median-comparable scenario.
                #
                # DISTINCT ON enforces the model's uniqueness key here rather
                # than trusting the source files to be duplicate-free.
                cursor.execute(
                    """
                    WITH base AS (
                        SELECT DISTINCT ON (account_number, tax_unit_code)
                            account_number, tax_year, county, tax_unit_code, tax_unit_name,
                            taxable_value AS gross, hcad_taxable AS net, source
                        FROM jur_exempt_stage
                        WHERE exemption_code = ''
                        ORDER BY account_number, tax_unit_code
                    ),
                    codes AS (
                        SELECT
                            account_number,
                            tax_unit_code,
                            left(
                                string_agg(DISTINCT exemption_code, '+' ORDER BY exemption_code),
                                32
                            ) AS code,
                            left(
                                string_agg(DISTINCT exemption_description, ' + '), 255
                            ) AS descr
                        FROM jur_exempt_stage
                        WHERE exemption_code <> ''
                        GROUP BY account_number, tax_unit_code
                    )
                    INSERT INTO data_propertyjurisdictionexemption (
                        account_number, tax_year, county, tax_unit_code, tax_unit_name,
                        exemption_code, exemption_description, exemption_amount,
                        exemption_percent, taxable_value, assessed_value, source,
                        created_at, updated_at
                    )
                    SELECT
                        b.account_number, b.tax_year, b.county, b.tax_unit_code, b.tax_unit_name,
                        '', '',
                        NULL::numeric, NULL::numeric,
                        b.gross, b.gross, b.source, %s, %s
                    FROM base b
                    UNION ALL
                    SELECT
                        b.account_number, b.tax_year, b.county, b.tax_unit_code, b.tax_unit_name,
                        coalesce(c.code, 'EXEMPT'),
                        coalesce(
                            c.descr, 'Exemption applied by HCAD (category not itemised)'
                        ),
                        b.gross - b.net, NULL::numeric,
                        NULL::numeric, NULL::numeric, b.source, %s, %s
                    FROM base b
                    LEFT JOIN codes c
                        ON c.account_number = b.account_number
                       AND c.tax_unit_code = b.tax_unit_code
                    WHERE b.net IS NOT NULL AND b.gross IS NOT NULL AND b.gross > b.net
                    """,
                    [stamp, stamp, stamp, stamp],
                )
                inserted = cursor.rowcount

                wrong = self._verify_applied(cursor, tax_year)
                if wrong:
                    raise CommandError(
                        f"Post-insert verification failed on {wrong:,} (account, unit) pairs: "
                        "stored gross minus stored exemptions does not reproduce HCAD's "
                        "taxable_val. Rolling back."
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f"Harris jurisdiction data for {tax_year}: replaced {deleted:,} rows with "
                f"{inserted:,}, every one reproducing HCAD's own taxable value."
            )
        )
