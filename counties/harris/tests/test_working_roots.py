"""Harris working sources stay under the roots that source retention manages."""

import io
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TransactionTestCase
from django.utils import timezone

from counties.common.models import ImportOperation
from counties.harris.etl_pipeline.tests.test_gis_source_layout import CANONICAL_ONLY, _write_layout
from counties.harris.etl_pipeline.tests.test_harris_import import _runtime_settings


class HarrisWorkingRootTests(TransactionTestCase):
    def test_working_sources_stay_managed_when_the_etl_root_variables_point_elsewhere(self):
        with (
            tempfile.TemporaryDirectory() as root,
            self.settings(**_runtime_settings(root)),
            patch.dict(
                os.environ,
                {
                    "ETL_DOWNLOAD_DIR": str(Path(root) / "elsewhere" / "downloads"),
                    "ETL_EXTRACT_DIR": str(Path(root) / "elsewhere" / "extracted"),
                },
            ),
            patch(
                "counties.harris.management.commands.load_gis_data.load_gis_parcels",
                return_value=1,
            ),
        ):
            _write_layout(root, CANONICAL_ONLY)
            call_command("load_gis_data", "--skip-download", stdout=io.StringIO())
            operation = ImportOperation.objects.get(county="harris")
            working = [Path(path) for path in operation.evidence["working_sources"]]
            self.assertTrue(working and all(path.is_dir() for path in working))
            ImportOperation.objects.filter(pk=operation.pk).update(
                finished_at=timezone.now() - timedelta(days=91)
            )

            call_command(
                "cleanup_import_sources", county="harris", reason="Expired", stdout=io.StringIO()
            )

            audit = operation.audit_entries.get(kind="working_source_cleanup")
            results = [source["result"] for source in audit.evidence["working_sources"]]
            # Retention manages both roots. It keeps the one holding the layer the load
            # recorded as a source, and reclaims the other.
            self.assertEqual(len(results), len(working))
            self.assertNotIn("unmanaged", results)
            self.assertIn("removed", results)
