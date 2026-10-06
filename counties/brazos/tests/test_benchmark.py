"""The Brazos operator benchmark runs a real, audited, prepare-only import."""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase

from counties.common.import_writers import WriterConflict
from counties.common.models import CountyWriter, ImportCandidate, ImportOperation

COMMAND = "benchmark_brazos_pipeline"


class BrazosBenchmarkCommandTests(TransactionTestCase):
    def _schemas(self) -> set[str]:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name LIKE 'brazos_candidate_%%'"
            )
            return {row[0] for row in cursor.fetchall()}

    def test_benchmark_records_an_import_operation_and_discards_its_candidate(self):
        schemas_before = self._schemas()
        out = StringIO()

        call_command(COMMAND, "--rows", "3", stdout=out)

        output = out.getvalue()
        self.assertIn("Benchmark completed successfully:", output)
        self.assertIn("  County:      brazos", output)
        self.assertIn("  Rows:        3", output)
        self.assertIn("  Status:      awaiting_review", output)
        operation = ImportOperation.objects.get(county="brazos")
        self.assertEqual(operation.status, "awaiting_review")
        self.assertIsNotNone(operation.finished_at)
        self.assertFalse(ImportCandidate.objects.filter(county="brazos").exists())
        self.assertEqual(self._schemas(), schemas_before)

    def test_benchmark_takes_the_real_county_writer_lock(self):
        holder = ImportOperation.objects.create(county="brazos", intent="preview")
        CountyWriter.objects.create(county="brazos", operation=holder)

        with self.assertRaises(WriterConflict):
            call_command(COMMAND, "--rows", "3", stdout=StringIO())

        attempt = ImportOperation.objects.exclude(pk=holder.pk).get(county="brazos")
        self.assertEqual(attempt.status, "failed")
