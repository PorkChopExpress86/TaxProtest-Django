from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from counties.harris.models import PropertyJurisdictionExemption


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

        with file_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh, delimiter=delimiter)
            if reader.fieldnames is None:
                raise CommandError("Input file has no header row")

            upserted = 0
            skipped = 0
            for row in reader:
                account_number = (
                    row.get("account_number") or row.get("acct") or row.get("account") or ""
                ).strip()
                tax_unit_code = (
                    row.get("tax_unit_code") or row.get("tax_unit") or row.get("unit_code") or ""
                ).strip()
                exemption_code = (row.get("exemption_code") or row.get("exempt_code") or "").strip()
                if not account_number or not tax_unit_code:
                    skipped += 1
                    continue

                PropertyJurisdictionExemption.objects.update_or_create(
                    account_number=account_number,
                    tax_year=tax_year,
                    tax_unit_code=tax_unit_code,
                    exemption_code=exemption_code,
                    county="harris",
                    defaults={
                        "tax_unit_name": (
                            row.get("tax_unit_name") or row.get("unit_name") or ""
                        ).strip(),
                        "exemption_description": (
                            row.get("exemption_description") or row.get("exempt_desc") or ""
                        ).strip(),
                        "exemption_amount": (
                            row.get("exemption_amount") or row.get("exempt_amt") or None
                        ),
                        "exemption_percent": (
                            row.get("exemption_percent") or row.get("exempt_pct") or None
                        ),
                        "taxable_value": (row.get("taxable_value") or row.get("taxable") or None),
                        "assessed_value": (
                            row.get("assessed_value") or row.get("assessed") or None
                        ),
                        "source": source,
                    },
                )
                upserted += 1

        if skipped and not upserted:
            # Feeding this command HCAD's raw jur_value.txt/jur_exempt.txt hits
            # exactly this: their columns are tax_district/exempt_cat, which none
            # of the aliases above match, so every row is skipped. Fail loudly
            # rather than report a successful no-op.
            raise CommandError(
                f"Every one of {skipped} rows was skipped for missing an account number or "
                f"tax unit code. Recognised headers are {sorted(reader.fieldnames or [])}; "
                "an account column must be named account_number/acct/account and a unit "
                "column tax_unit_code/tax_unit/unit_code. To load HCAD's own "
                "Real_jur_exempt files, use import_hcad_jur_exempt instead."
            )
        if skipped:
            self.stdout.write(
                self.style.WARNING(f"Skipped {skipped} rows missing an account or tax unit code.")
            )
        self.stdout.write(
            self.style.SUCCESS(f"Upserted {upserted} jurisdiction/exemption rows for {tax_year}.")
        )
