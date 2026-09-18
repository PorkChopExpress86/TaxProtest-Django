"""Clean only managed sources whose recorded retirement is at least 90 days old."""

from django.core.management.base import BaseCommand

from counties.common.import_retention import cleanup_sources


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--county", choices=("harris", "brazos"), required=True)
        parser.add_argument("--actor", default="")
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        operation = cleanup_sources(
            options["county"], actor=options["actor"], reason=options["reason"]
        )
        self.stdout.write(f"Source cleanup operation: {operation.pk}; audit available in admin")
