import json
from django.core.management.base import BaseCommand

from data.tasks_new import download_and_extract_hcad


class Command(BaseCommand):
    help = "Download and extract HCAD archives into downloads/"

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING("Starting HCAD download & extract..."))
        res = download_and_extract_hcad.run()
        self.stdout.write(self.style.SUCCESS("Done."))
        self.stdout.write(json.dumps(res, indent=2))
