from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from data.models import PropertyJurisdictionExemption


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
            for row in reader:
                account_number = (row.get("account_number") or row.get("acct") or row.get("account") or "").strip()
                tax_unit_code = (row.get("tax_unit_code") or row.get("tax_unit") or row.get("unit_code") or "").strip()
                exemption_code = (row.get("exemption_code") or row.get("exempt_code") or "").strip()
                if not account_number or not tax_unit_code:
                    continue

                PropertyJurisdictionExemption.objects.update_or_create(
                    account_number=account_number,
                    tax_year=tax_year,
                    tax_unit_code=tax_unit_code,
                    exemption_code=exemption_code,
                    defaults={
                        "tax_unit_name": (row.get("tax_unit_name") or row.get("unit_name") or "").strip(),
                        "exemption_description": (row.get("exemption_description") or row.get("exempt_desc") or "").strip(),
                        "exemption_amount": (row.get("exemption_amount") or row.get("exempt_amt") or None),
                        "exemption_percent": (row.get("exemption_percent") or row.get("exempt_pct") or None),
                        "taxable_value": (row.get("taxable_value") or row.get("taxable") or None),
                        "assessed_value": (row.get("assessed_value") or row.get("assessed") or None),
                        "source": source,
                    },
                )
                upserted += 1

        self.stdout.write(self.style.SUCCESS(f"Upserted {upserted} jurisdiction/exemption rows for {tax_year}."))
