from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from data.models import TaxUnitRate


class Command(BaseCommand):
    help = "Import annual tax-unit rates with upsert semantics"

    def add_arguments(self, parser):
        parser.add_argument("--path", required=True)
        parser.add_argument("--tax-year", type=int, required=True)
        parser.add_argument("--delimiter", default="\t")
        parser.add_argument("--source", default="manual_import")

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
                code = (row.get("tax_unit_code") or row.get("tax_unit") or row.get("unit_code") or "").strip()
                name = (row.get("tax_unit_name") or row.get("unit_name") or "").strip()
                rate_text = (row.get("adopted_rate") or row.get("rate") or "").strip()
                if not code or not rate_text:
                    continue

                TaxUnitRate.objects.update_or_create(
                    tax_year=tax_year,
                    tax_unit_code=code,
                    defaults={
                        "tax_unit_name": name,
                        "adopted_rate": rate_text,
                        "source": source,
                    },
                )
                upserted += 1

        self.stdout.write(self.style.SUCCESS(f"Upserted {upserted} tax-unit rate rows for {tax_year}."))
