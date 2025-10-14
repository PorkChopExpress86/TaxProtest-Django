import os
from django.core.management.base import BaseCommand, CommandError

from data.etl import bulk_load_properties


class Command(BaseCommand):
    help = "Load HCAD Real Account (real_acct.txt) into PropertyRecord table."

    def add_arguments(self, parser):
        parser.add_argument(
            "filepath",
            nargs="?",
            help="Path to real_acct.txt (default: downloads/Real_acct_owner/real_acct.txt)",
        )
        parser.add_argument("--chunk", type=int, default=5000, help="Bulk insert chunk size")
        parser.add_argument("--limit", type=int, default=None, help="Limit number of rows to insert (for testing)")

    def handle(self, *args, **options):
        filepath = options.get("filepath") or os.path.join(
            os.getcwd(), "downloads", "Real_acct_owner", "real_acct.txt"
        )
        if not os.path.exists(filepath):
            raise CommandError(f"File not found: {filepath}")
        self.stdout.write(self.style.WARNING(f"Loading properties from: {filepath}"))
        count = bulk_load_properties(filepath, chunk_size=options["chunk"], limit=options.get("limit"))
        self.stdout.write(self.style.SUCCESS(f"Inserted {count} PropertyRecord rows."))
