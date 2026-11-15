"""The staged load is not reached when source validation fails (#179).

Each test calls the Harris import's single entry point for real, against temporary source
files and PostgreSQL, with a valid property source and a broken building-land source. The
property file is loaded first, so a staged load that ran would call the persistence seam
(ADR-0007); a spy records every call without changing it.
"""

from __future__ import annotations

import shutil
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings

from counties.common.models import ImportOperation
from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisApply,
    HarrisExtractionMode,
    HarrisFailurePolicy,
    HarrisImportPhase,
    HarrisImportRequest,
    HarrisImportStatus,
    HarrisPrepare,
    run_harris_import,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.persistence import HarrisPersistence
from counties.harris.etl_pipeline.tests.test_harris_import import (
    _runtime_settings,
    _write_building_sources,
    _write_property_source,
    extracted,
)
from counties.harris.models import PropertyRecord
from counties.harris.source_catalog import HcadSourceId

ACCOUNT = "V179"
LOADS = {
    "apply": HarrisApply(refresh_readiness=False, validate_completeness=False),
    "prepare": HarrisPrepare(refresh_readiness=False, validate_completeness=False),
}


class StagedLoadNeedsPassingValidationTests(TestCase):
    def _run_each_way(self, break_building_source, expected_error: str) -> None:
        for policy in HarrisFailurePolicy:
            for name, load in LOADS.items():
                with self.subTest(policy=policy.name, load=name):
                    persisted = []
                    persist = HarrisPersistence.persist

                    def recording_persist(self, request, persisted=persisted, persist=persist):
                        persisted.append(request.dataset)
                        return persist(self, request)

                    with (
                        tempfile.TemporaryDirectory() as root,
                        override_settings(**_runtime_settings(root)),
                        patch.object(HarrisPersistence, "persist", recording_persist),
                    ):
                        _write_property_source(root, account=ACCOUNT)
                        _write_building_sources(root, account=ACCOUNT)
                        break_building_source(root)
                        result = run_harris_import(
                            HarrisImportRequest(
                                plan=HarrisImportPlan.from_legacy_scope("property-and-building"),
                                acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
                                extraction=HarrisExtractionMode.REUSE_EXTRACTED,
                                load=load,
                                failure_policy=policy,
                            )
                        )

                    validation = result.stages[HarrisImportPhase.SOURCE_VALIDATION]
                    self.assertFalse(validation.success)
                    self.assertEqual(validation.error, expected_error)
                    self.assertNotIn(HarrisImportPhase.LOAD, result.stages)
                    self.assertEqual(persisted, [])
                    self.assertIs(result.status, HarrisImportStatus.FAILED)
                    self.assertFalse(result.wrote_data)
                    self.assertEqual(result.errors, (expected_error,))
                    stored = ImportOperation.objects.get(pk=result.operation_id).evidence
                    self.assertEqual(stored["validation"]["errors"], [expected_error])
                    self.assertFalse(PropertyRecord.objects.filter(account_number=ACCOUNT).exists())

    def test_a_missing_required_member_fails_validation_before_the_load(self):
        def remove_fixtures(root: str) -> None:
            (extracted(root, HcadSourceId.REAL_BUILDING_LAND) / "fixtures.txt").unlink()

        self._run_each_way(
            remove_fixtures, "Real Building Land: required files missing: fixtures.txt"
        )

    def test_an_absent_required_extract_directory_fails_validation_before_the_load(self):
        def remove_directory(root: str) -> None:
            shutil.rmtree(extracted(root, HcadSourceId.REAL_BUILDING_LAND))

        self._run_each_way(
            remove_directory,
            "Real Building Land: required files missing: "
            "building_res.txt, fixtures.txt, extra_features*.txt",
        )
