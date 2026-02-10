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
        parser.add_argument(
            "--truncate",
            action="store_true",
            default=True,
            help="Truncate table before import (default: True)",
        )
        parser.add_argument(
            "--no-truncate",
            action="store_true",
            help="Do NOT truncate table before import (append to existing data)",
        )

    def handle(self, *args, **options):
        filepath = options.get("filepath") or os.path.join(
            os.getcwd(), "downloads", "Real_acct_owner", "real_acct.txt"
        )
        if not os.path.exists(filepath):
            raise CommandError(f"File not found: {filepath}")
        
        # Handle truncate flag (--no-truncate overrides --truncate)
        truncate = not options.get("no_truncate", False)
        
        if truncate:
            self.stdout.write(self.style.WARNING("Table will be TRUNCATED before import."))
        else:
            self.stdout.write(self.style.WARNING("Appending to existing data (no truncate)."))
        
        self.stdout.write(self.style.WARNING(f"Loading properties from: {filepath}"))
        count = bulk_load_properties(
            filepath, 
            chunk_size=options["chunk"], 
            limit=options.get("limit"),
            truncate=truncate
        )
        self.stdout.write(self.style.SUCCESS(f"Inserted {count} PropertyRecord rows."))

