"""What source validation approves is what the staged load loads (#176).

Every test calls the Harris import's single entry point against a catalog-shaped extract:
each source's declared members at the top level of its catalog extract directory, beside a
retained archive holding the same bytes. The extract carries skipped, invalid and duplicate
rows in every dataset, detail files that supersede ``extra_features.txt``, room counts, and
building and feature rows whose accounts the property file excludes (non-residential) or
lacks.

The load is observed through what it staged in the candidate schema and through the
persistence seam's per-dataset results (ADR-0007), which a spy records without changing.
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from counties.common.candidate_staging import switch_search_path
from counties.common.models import ImportCandidate, ImportOperation
from counties.harris.etl_pipeline import (
    HarrisAcquisitionMode,
    HarrisExtractionMode,
    HarrisImportPhase,
    HarrisImportRequest,
    HarrisImportStatus,
    HarrisPrepare,
    HarrisPreview,
    run_harris_import,
)
from counties.harris.etl_pipeline.import_plan import HarrisImportPlan
from counties.harris.etl_pipeline.persistence import HarrisPersistence, PersistenceDataset
from counties.harris.models import BuildingDetail, ExtraFeature, PropertyRecord
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, HcadSourceId

DATA_YEAR = 2026

REAL_ACCT = (
    "acct\tyr\tsite_addr_1\tsite_addr_3\tstate_class\ttot_appr_val\n"
    "R001\t2026\t1 OAK ST\t77001\tA1\t250000\n"
    "R002\t2026\t2 OAK ST\t77001\tA1\t300000\n"
    # Non-residential: skipped, so its building and feature rows have no account.
    "C001\t2026\t3 MAIN ST\t77002\tF1\t900000\n"
    # An identical duplicate.
    "R001\t2026\t1 OAK ST\t77001\tA1\t250000\n"
    # A stray tab shifts the fields.
    "R003\t2026\t4 OAK ST\t\t77001\tA1\t100000\n"
)
OWNERS = "acct\tln_num\tname\nR001\t1\tSMITH JOHN\n"
BUILDING_RES = (
    "acct\tbld_num\timprv_type\theat_ar\tbed_rm\tfull_bath\thalf_bath\n"
    # The fixtures file, not bed_rm, supplies this building's room counts.
    "R001\t1\t1001\t1800\t9\t9\t9\n"
    "R001\t1\t1001\t1800\t9\t9\t9\n"
    "R002\t1\t1001\t1500\t2\t1\t0\n"
    "C001\t1\t4300\t9000\t0\t0\t0\n"
    # No such account in the property file.
    "X999\t1\t1001\t1200\t3\t2\t0\n"
    "R002\t1\t\t1001\t1500\t2\t1\t0\n"
    # Every field empty: the layout inspection passes over it and translation skips it.
    "\t\t\t\t\t\t\n"
)
FIXTURES = (
    "acct\tbld_num\ttype\ttype_dscr\tunits\n"
    "R001\t1\tRMB\tRoom: Bedroom\t3.00\n"
    "R001\t1\tRMF\tRoom: Full Bath\t2.00\n"
    "R001\t1\tRMH\tRoom: Half Bath\t1.00\n"
    "R001\t1\tWH\tWater Heater\t1.00\n"
    # A room type repeated with different units has no knowable count.
    "R002\t1\tRMB\tRoom: Bedroom\t3.00\n"
    "R002\t1\tRMB\tRoom: Bedroom\t4.00\n"
    "R002\t2\t    \t\t0.00\n"
    "R002\t1\t\tRMF\tRoom: Full Bath\t1.00\n"
)
# Superseded by the detail files: neither validated nor loaded.
EXTRA_FEATURES = "acct\tbld_num\tcd\tdscr\tasd_val\nR001\t0\tOLDX\tSuperseded\t1\n"
EXTRA_FEATURES_DETAIL_1 = (
    "acct\tbld_num\tcd\tdscr\tasd_val\n"
    "R001\t1\tPOOL\tPool\t25000\n"
    "R001\t0\tRRS1\tShed\t1500\n"
    # HCAD has no feature sequence: an identical second shed is a second feature.
    "R001\t0\tRRS1\tShed\t1500\n"
    "R002\t0\tCPA1\tPaving\tPending\n"
    "C001\t1\tGAR\tGarage\t5000\n"
    "X999\t1\tGAR\tGarage\t5000\n"
    "R002\t\tCPA2\tPaving\t100\n"
    "R002\t1\t\tUnknown\t100\n"
    "R002\t1\tGAR\t\tGarage\t100\n"
)
EXTRA_FEATURES_DETAIL_2 = (
    "acct\tbld_num\tcd\tdscr\tasd_val\n"
    # The third shed of R001: its ordinal continues across the detail files.
    "R001\t0\tRRS1\tShed\t1500\n"
    "R002\t1\tSHD1\tStorage\t800\n"
    "R002\t1\tSHD2\t\tStorage\t800\n"
)
LAND = "acct\tnum\tuse_cd\nR001\t1\t1001\n"


def _catalog_extract(**overrides: str | None) -> dict[HcadSourceId, dict[str, str]]:
    members = {
        HcadSourceId.REAL_ACCOUNT_OWNER: {"owners.txt": OWNERS, "real_acct.txt": REAL_ACCT},
        HcadSourceId.REAL_BUILDING_LAND: {
            "building_res.txt": BUILDING_RES,
            "extra_features.txt": EXTRA_FEATURES,
            "extra_features_detail1.txt": EXTRA_FEATURES_DETAIL_1,
            "extra_features_detail2.txt": EXTRA_FEATURES_DETAIL_2,
            "fixtures.txt": FIXTURES,
            "land.txt": LAND,
        },
    }
    for name, text in overrides.items():
        filename = f"{name}.txt"
        for files in members.values():
            if filename in files:
                if text is None:
                    del files[filename]
                else:
                    files[filename] = text
    return members


def _write_extract(root: str, members: dict[HcadSourceId, dict[str, str]]) -> None:
    downloads = Path(root) / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    for source_id, files in members.items():
        directory = (
            Path(root) / "extracted" / DEFAULT_HCAD_SOURCE_CATALOG.extract_dirname(source_id)
        )
        directory.mkdir(parents=True, exist_ok=True)
        archive = downloads / DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(source_id).filename
        # Stored members with a fixed timestamp keep the archive's digest fixed.
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as contents:
            for filename, text in files.items():
                data = text.encode("latin-1")
                (directory / filename).write_bytes(data)
                contents.writestr(zipfile.ZipInfo(filename, date_time=(2026, 1, 1, 0, 0, 0)), data)


def _settings(root: str) -> dict[str, str]:
    base = Path(root)
    return {
        "BASE_DIR": root,
        "HCAD_DOWNLOAD_DIR": str(base / "downloads"),
        "HCAD_EXTRACT_DIR": str(base / "extracted"),
        "HCAD_LOG_DIR": str(base / "logs"),
    }


def _request(load: HarrisPrepare | HarrisPreview) -> HarrisImportRequest:
    return HarrisImportRequest(
        plan=HarrisImportPlan.from_legacy_scope("property-and-building"),
        data_year=DATA_YEAR,
        acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
        extraction=HarrisExtractionMode.REUSE_EXTRACTED,
        load=load,
    )


def _prepare() -> HarrisPrepare:
    return HarrisPrepare(refresh_readiness=False, validate_completeness=False)


def _pinned_text(operation: ImportOperation, root: str) -> str:
    """Canonical JSON of the stored validation evidence and recorded sources.

    Only the temporary root and the operation id, which name the working roots, are
    replaced; every other byte is compared.
    """
    stored = ImportOperation.objects.get(pk=operation.pk).evidence
    text = json.dumps(
        {"sources": stored["sources"], "validation": stored["validation"]},
        indent=1,
        sort_keys=True,
    )
    return text.replace(str(root), "{root}").replace(str(operation.pk), "{operation}")


_SCHEMA_DATASETS = {
    "real_acct": PersistenceDataset.PROPERTY,
    "building_res": PersistenceDataset.BUILDING,
    "extra_features": PersistenceDataset.EXTRA_FEATURE,
}


class ValidatedEqualsLoadedTests(TestCase):
    # Keep the candidate's schema to read what the load staged.
    @patch("counties.common.candidate_lifecycle.drop_candidate_schema")
    def test_validated_files_and_counts_are_what_the_staged_load_loaded(self, _drop):
        persisted = []
        persist = HarrisPersistence.persist

        def recording_persist(self, request):
            result = persist(self, request)
            persisted.append(result)
            return result

        with (
            tempfile.TemporaryDirectory() as root,
            override_settings(**_settings(root)),
            patch.object(HarrisPersistence, "persist", recording_persist),
        ):
            _write_extract(root, _catalog_extract())
            result = run_harris_import(_request(_prepare()))

        validation = ImportOperation.objects.get(pk=result.operation_id).evidence["validation"]
        self.assertTrue(validation["valid"], validation["errors"])
        self.assertTrue(result.stages[HarrisImportPhase.LOAD].success, result.errors)

        candidate = ImportCandidate.objects.get(pk=result.candidate_id)
        with switch_search_path(candidate.storage_schema):
            staged = {
                PersistenceDataset.PROPERTY: PropertyRecord.objects.count(),
                PersistenceDataset.BUILDING: BuildingDetail.objects.count(),
                PersistenceDataset.EXTRA_FEATURE: ExtraFeature.objects.count(),
            }
            feature_codes = set(ExtraFeature.objects.values_list("feature_code", flat=True))
            shed_ordinals = sorted(
                ExtraFeature.objects.filter(account_number="R001", feature_code="RRS1").values_list(
                    "source_ordinal", flat=True
                )
            )
            rooms = BuildingDetail.objects.values_list("bedrooms", "bathrooms", "half_baths").get(
                account_number="R001", building_number=1
            )
            loaded_files = {
                "real_acct.txt": PropertyRecord.objects.exists(),
                "building_res.txt": BuildingDetail.objects.exists(),
                # bed_rm says 9; only fixtures.txt says 3 bedrooms and 2.5 baths.
                "fixtures.txt": rooms == (3, 2.5, 1),
                "extra_features.txt": "OLDX" in feature_codes,
                "extra_features_detail1.txt": "POOL" in feature_codes,
                "extra_features_detail2.txt": "SHD1" in feature_codes,
            }

        # The files validation selected are the files the load read.
        self.assertEqual(
            sorted(Path(file["path"]).name for file in validation["files"]),
            sorted(name for name, loaded in loaded_files.items() if loaded),
        )
        # Each dataset's validated counts are what the load persisted and staged.
        self.assertEqual(
            {outcome.dataset for outcome in persisted}, set(PersistenceDataset), persisted
        )
        self.assertEqual(len(persisted), 3, persisted)
        loaded = {
            schema: next(
                {"loaded": outcome.loaded, "invalid": outcome.invalid, "skipped": outcome.skipped}
                for outcome in persisted
                if outcome.dataset is dataset
            )
            for schema, dataset in _SCHEMA_DATASETS.items()
        }
        self.assertEqual(validation["translated"], loaded)
        self.assertEqual(
            {schema: counts["loaded"] for schema, counts in validation["translated"].items()},
            {schema: staged[dataset] for schema, dataset in _SCHEMA_DATASETS.items()},
        )
        load_metrics = result.stages[HarrisImportPhase.LOAD].metrics
        for metric, count in (
            ("records_loaded", "loaded"),
            ("records_invalid", "invalid"),
            ("records_skipped", "skipped"),
        ):
            self.assertEqual(
                load_metrics[metric],
                sum(counts[count] for counts in validation["translated"].values()),
                metric,
            )
        # Worked by hand from the extract's rows, so equal zeros cannot pass.
        self.assertEqual(
            validation["translated"],
            {
                "real_acct": {"loaded": 2, "invalid": 1, "skipped": 2},
                "building_res": {"loaded": 2, "invalid": 3, "skipped": 2},
                "extra_features": {"loaded": 6, "invalid": 6, "skipped": 0},
            },
        )
        self.assertEqual(shed_ordinals, [1, 2, 3])


class StoredValidationEvidenceTests(TestCase):
    """The stored validation evidence and recorded sources, pinned byte for byte (ADR-0024).

    A held candidate binds the valid extract's bytes; the failing extracts pin validation's
    error text and order. Never regenerate a literal to make a failure pass.
    """

    def evidence(self, members, load) -> str:
        with tempfile.TemporaryDirectory() as root, override_settings(**_settings(root)):
            _write_extract(root, members)
            result = run_harris_import(_request(load))
            return _pinned_text(ImportOperation.objects.get(pk=result.operation_id), root)

    @patch("counties.common.candidate_lifecycle.drop_candidate_schema")
    def test_a_catalog_shaped_extract_stores_the_pinned_bytes(self, _drop):
        self.assertEqual(self.evidence(_catalog_extract(), _prepare()), PINNED_VALID)

    @patch("counties.common.candidate_lifecycle.drop_candidate_schema")
    def test_a_missing_member_and_a_layout_defect_store_the_pinned_bytes(self, _drop):
        members = _catalog_extract(
            fixtures=None,
            real_acct=REAL_ACCT + "R004\t2026\t5 OAK ST\t77001\tA1\tnot a number\n",
        )
        self.assertEqual(self.evidence(members, _prepare()), PINNED_LAYOUT_FAILURE)

    @patch("counties.common.candidate_lifecycle.drop_candidate_schema")
    def test_a_conflicting_identity_stores_the_pinned_bytes(self, _drop):
        members = _catalog_extract(
            real_acct=REAL_ACCT + "R002\t2026\t2 OAK ST\t77001\tA1\t310000\n",
        )
        self.assertEqual(self.evidence(members, _prepare()), PINNED_CONFLICT)


class ValidatedImportPreviewTests(TestCase):
    def test_preview_validates_the_sources_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as root, override_settings(**_settings(root)):
            _write_extract(root, _catalog_extract())
            result = run_harris_import(_request(HarrisPreview()))
            operation = ImportOperation.objects.get(pk=result.operation_id)
            pinned = _pinned_text(operation, root)

        self.assertIs(result.status, HarrisImportStatus.COMPLETED, result.errors)
        self.assertFalse(result.wrote_data)
        self.assertIsNone(result.candidate_id)
        # The same validation evidence an import stores, sources and all.
        self.assertTrue(operation.evidence["validation"]["valid"])
        self.assertTrue(operation.evidence["validation"]["files"])
        self.assertEqual(pinned, PINNED_VALID)
        self.assertFalse(ImportCandidate.objects.exists())
        self.assertFalse(PropertyRecord.objects.exists())
        self.assertFalse(BuildingDetail.objects.exists())
        self.assertFalse(ExtraFeature.objects.exists())


PINNED_VALID = """{
 "sources": [
  {
   "path": "{root}/downloads/.imports/{operation}/Real_acct_owner.zip",
   "requested_year": 2026,
   "sha256": "4e6ec8fab972f43f5985083b559428e31039d4d96bac459cb8bfb5378e1b6350",
   "source_id": "real-account-owner",
   "source_url": null,
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_acct_owner/real_acct.txt",
   "requested_year": 2026,
   "sha256": "fa06dfed57aabb56873c422300c6f378ccd9a078bba53d18c02cbe3aa83d444a",
   "source_id": "real-account-owner",
   "source_year": null
  },
  {
   "path": "{root}/downloads/.imports/{operation}/Real_building_land.zip",
   "requested_year": 2026,
   "sha256": "afdbb77faa2031631daa59f5b4b7c979d7258c2ff74d5e84ac3dfc179a476b74",
   "source_id": "real-building-land",
   "source_url": null,
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_building_land/building_res.txt",
   "requested_year": 2026,
   "sha256": "4c2a335ea5a98db87f07de316865ec3d0de582dff13f9438375ad64870395c26",
   "source_id": "real-building-land",
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_building_land/extra_features_detail1.txt",
   "requested_year": 2026,
   "sha256": "6f200427fd1e2cb6705738b40a7e81235ae0cd0dd434af3e151b26e60f069f9a",
   "source_id": "real-building-land",
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_building_land/extra_features_detail2.txt",
   "requested_year": 2026,
   "sha256": "3d0787d92091c14d09e550b4a92a47f6467e91059467bfc942255c355cce7c73",
   "source_id": "real-building-land",
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_building_land/fixtures.txt",
   "requested_year": 2026,
   "sha256": "9d49202a9b84e2002ea60cf9ba462f4c624691c1340f246cdf4c7ef2ebf26f68",
   "source_id": "real-building-land",
   "source_year": null
  }
 ],
 "validation": {
  "database_publication": "Database publication untested",
  "errors": [],
  "files": [
   {
    "invalid": 1,
    "observed_source_years": [
     2026
    ],
    "path": "{root}/extracted/.imports/{operation}/Real_acct_owner/real_acct.txt",
    "records": 5,
    "schema": "real_acct"
   },
   {
    "invalid": 1,
    "observed_source_years": [],
    "path": "{root}/extracted/.imports/{operation}/Real_building_land/building_res.txt",
    "records": 6,
    "schema": "building_res"
   },
   {
    "invalid": 1,
    "observed_source_years": [],
    "path": "{root}/extracted/.imports/{operation}/Real_building_land/extra_features_detail1.txt",
    "records": 9,
    "schema": "extra_features"
   },
   {
    "invalid": 1,
    "observed_source_years": [],
    "path": "{root}/extracted/.imports/{operation}/Real_building_land/extra_features_detail2.txt",
    "records": 3,
    "schema": "extra_features"
   },
   {
    "invalid": 4,
    "observed_source_years": [],
    "path": "{root}/extracted/.imports/{operation}/Real_building_land/fixtures.txt",
    "records": 8,
    "schema": "fixtures"
   }
  ],
  "translated": {
   "building_res": {
    "invalid": 3,
    "loaded": 2,
    "skipped": 2
   },
   "extra_features": {
    "invalid": 6,
    "loaded": 6,
    "skipped": 0
   },
   "real_acct": {
    "invalid": 1,
    "loaded": 2,
    "skipped": 2
   }
  },
  "valid": true
 }
}"""
PINNED_LAYOUT_FAILURE = """{
 "sources": [
  {
   "path": "{root}/downloads/.imports/{operation}/Real_acct_owner.zip",
   "requested_year": 2026,
   "sha256": "4856a80ce2221bc5c5dc07fef9d43f412d2e0b9b862335f3cf963afe3eeb1152",
   "source_id": "real-account-owner",
   "source_url": null,
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_acct_owner/real_acct.txt",
   "requested_year": 2026,
   "sha256": "ef87e72a8d24e6362e9c4c3b932bc8fd082dcf4641726bad3ae05fd571a19764",
   "source_id": "real-account-owner",
   "source_year": null
  },
  {
   "path": "{root}/downloads/.imports/{operation}/Real_building_land.zip",
   "requested_year": 2026,
   "sha256": "4f793fcf0e19a41453638bd65b6ae323f7c26984b3d2e289905e2d6121ae5ad2",
   "source_id": "real-building-land",
   "source_url": null,
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_building_land/building_res.txt",
   "requested_year": 2026,
   "sha256": "4c2a335ea5a98db87f07de316865ec3d0de582dff13f9438375ad64870395c26",
   "source_id": "real-building-land",
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_building_land/extra_features_detail1.txt",
   "requested_year": 2026,
   "sha256": "6f200427fd1e2cb6705738b40a7e81235ae0cd0dd434af3e151b26e60f069f9a",
   "source_id": "real-building-land",
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_building_land/extra_features_detail2.txt",
   "requested_year": 2026,
   "sha256": "3d0787d92091c14d09e550b4a92a47f6467e91059467bfc942255c355cce7c73",
   "source_id": "real-building-land",
   "source_year": null
  }
 ],
 "validation": {
  "database_publication": "Database publication untested",
  "errors": [
   "real_acct.txt:7: invalid numeric value",
   "Real Building Land: required files missing: fixtures.txt"
  ],
  "files": [
   {
    "invalid": 2,
    "observed_source_years": [
     2026
    ],
    "path": "{root}/extracted/.imports/{operation}/Real_acct_owner/real_acct.txt",
    "records": 6,
    "schema": "real_acct"
   },
   {
    "invalid": 1,
    "observed_source_years": [],
    "path": "{root}/extracted/.imports/{operation}/Real_building_land/building_res.txt",
    "records": 6,
    "schema": "building_res"
   },
   {
    "invalid": 1,
    "observed_source_years": [],
    "path": "{root}/extracted/.imports/{operation}/Real_building_land/extra_features_detail1.txt",
    "records": 9,
    "schema": "extra_features"
   },
   {
    "invalid": 1,
    "observed_source_years": [],
    "path": "{root}/extracted/.imports/{operation}/Real_building_land/extra_features_detail2.txt",
    "records": 3,
    "schema": "extra_features"
   }
  ],
  "translated": {},
  "valid": false
 }
}"""
PINNED_CONFLICT = """{
 "sources": [
  {
   "path": "{root}/downloads/.imports/{operation}/Real_acct_owner.zip",
   "requested_year": 2026,
   "sha256": "a6b5b14beabc76fe02e6c31388e7a1f498e52089611835a2f3a1601bd0b1b396",
   "source_id": "real-account-owner",
   "source_url": null,
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_acct_owner/real_acct.txt",
   "requested_year": 2026,
   "sha256": "388b562f5042a687dd1e191ad8f96c8c67378adb3900086e178cfa3783d17a5e",
   "source_id": "real-account-owner",
   "source_year": null
  },
  {
   "path": "{root}/downloads/.imports/{operation}/Real_building_land.zip",
   "requested_year": 2026,
   "sha256": "afdbb77faa2031631daa59f5b4b7c979d7258c2ff74d5e84ac3dfc179a476b74",
   "source_id": "real-building-land",
   "source_url": null,
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_building_land/building_res.txt",
   "requested_year": 2026,
   "sha256": "4c2a335ea5a98db87f07de316865ec3d0de582dff13f9438375ad64870395c26",
   "source_id": "real-building-land",
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_building_land/extra_features_detail1.txt",
   "requested_year": 2026,
   "sha256": "6f200427fd1e2cb6705738b40a7e81235ae0cd0dd434af3e151b26e60f069f9a",
   "source_id": "real-building-land",
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_building_land/extra_features_detail2.txt",
   "requested_year": 2026,
   "sha256": "3d0787d92091c14d09e550b4a92a47f6467e91059467bfc942255c355cce7c73",
   "source_id": "real-building-land",
   "source_year": null
  },
  {
   "path": "{root}/extracted/.imports/{operation}/Real_building_land/fixtures.txt",
   "requested_year": 2026,
   "sha256": "9d49202a9b84e2002ea60cf9ba462f4c624691c1340f246cdf4c7ef2ebf26f68",
   "source_id": "real-building-land",
   "source_year": null
  }
 ],
 "validation": {
  "database_publication": "Database publication untested",
  "errors": [
   "real_acct.txt: conflicting canonical real_acct identity ('R002',)"
  ],
  "files": [
   {
    "invalid": 1,
    "observed_source_years": [
     2026
    ],
    "path": "{root}/extracted/.imports/{operation}/Real_acct_owner/real_acct.txt",
    "records": 6,
    "schema": "real_acct"
   },
   {
    "invalid": 1,
    "observed_source_years": [],
    "path": "{root}/extracted/.imports/{operation}/Real_building_land/building_res.txt",
    "records": 6,
    "schema": "building_res"
   },
   {
    "invalid": 1,
    "observed_source_years": [],
    "path": "{root}/extracted/.imports/{operation}/Real_building_land/extra_features_detail1.txt",
    "records": 9,
    "schema": "extra_features"
   },
   {
    "invalid": 1,
    "observed_source_years": [],
    "path": "{root}/extracted/.imports/{operation}/Real_building_land/extra_features_detail2.txt",
    "records": 3,
    "schema": "extra_features"
   },
   {
    "invalid": 4,
    "observed_source_years": [],
    "path": "{root}/extracted/.imports/{operation}/Real_building_land/fixtures.txt",
    "records": 8,
    "schema": "fixtures"
   }
  ],
  "translated": {
   "building_res": {
    "invalid": 3,
    "loaded": 2,
    "skipped": 2
   },
   "extra_features": {
    "invalid": 6,
    "loaded": 6,
    "skipped": 0
   },
   "real_acct": {
    "invalid": 1,
    "loaded": 2,
    "skipped": 3
   }
  },
  "valid": false
 }
}"""
