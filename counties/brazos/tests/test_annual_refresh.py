"""Contract tests for the Brazos annual-refresh module."""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from counties.brazos.annual_refresh import (
    BrazosAnnualRefresh,
    RefreshOptions,
    StagePreparation,
    StageResult,
    build_default_refresh,
)
from counties.brazos.cad_refresh import CadRefreshStage
from counties.brazos.gis_refresh import GisRefreshStage
from counties.brazos.models import BrazosPropertySnapshot, PropertyAccount, SnapshotOutcome
from counties.brazos.stage_reporting import SilentStageReporter
from counties.common.models import ImportOperation


class _Stage:
    def __init__(self, name: str, *, source_year: int, target_year: int):
        self.name = name
        self.source_year = source_year
        self.target_year = target_year
        self.persisted = False
        self.cleaned = False

    def prepare(self, options: RefreshOptions) -> StagePreparation:
        return StagePreparation(
            name=self.name,
            source_year=self.source_year,
            target_year=self.target_year,
            payload=None,
            cleanup_paths=(Path(self.name),),
        )

    def persist(self, preparation: StagePreparation) -> StageResult:
        self.persisted = True
        return StageResult(name=self.name, metrics={"loaded": 1})

    def cleanup(self, preparation: StagePreparation) -> None:
        self.cleaned = True


class _ReplacingCadStage(_Stage):
    def persist(self, preparation: StagePreparation) -> StageResult:
        from counties.brazos.models import PropertyAccount

        self.persisted = True
        PropertyAccount.objects.filter(tax_year=preparation.target_year).delete()
        PropertyAccount.objects.create(
            prop_id="000000010013",
            tax_year=preparation.target_year,
            owner_name="Replacement owner",
            assessed_value=Decimal("250000"),
        )
        return StageResult(name=self.name, metrics={"loaded": 1})


class _FailingGisStage(_Stage):
    def persist(self, preparation: StagePreparation) -> StageResult:
        self.persisted = True
        raise RuntimeError("GIS enrichment failed")


class AnnualRefreshWiringTests(SimpleTestCase):
    def test_factory_constructs_county_owned_stages(self):
        refresh = build_default_refresh(SilentStageReporter())

        self.assertIsInstance(refresh._cad, CadRefreshStage)
        self.assertIsInstance(refresh._gis, GisRefreshStage)


class AnnualRefreshOnlineDryRunTests(SimpleTestCase):
    def test_discovery_selects_online_sources_without_claiming_a_validated_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cad = CadRefreshStage()
            gis = GisRefreshStage()

            with (
                self.settings(
                    BCAD_DOWNLOAD_DIR=str(root / "downloads"),
                    BCAD_EXTRACT_DIR=str(root / "extracted"),
                ),
                patch.object(
                    cad,
                    "_scrape_archive",
                    return_value=("https://example.test/cad.zip", 2026),
                ),
                patch.object(
                    gis,
                    "_scrape_archive",
                    return_value=("https://example.test/gis.zip", 2026),
                ),
            ):
                cad_source = cad.prepare(RefreshOptions(dry_run=True))
                gis_source = gis.prepare(RefreshOptions(tax_year=2026, dry_run=True))

            self.assertEqual(cad_source.target_year, 2026)
            self.assertEqual(gis_source.target_year, 2026)
            self.assertIsNone(gis_source.payload.shapefile_path)
            self.assertFalse((root / "downloads" / "bcad_certified_2026.zip").exists())
            self.assertFalse((root / "downloads" / "bcad_gis_2026.zip").exists())


class AnnualRefreshYearContractTests(TestCase):
    def test_source_year_mismatch_stops_before_any_persistence(self):
        cad = _Stage("cad", source_year=2025, target_year=2025)
        gis = _Stage("gis", source_year=2024, target_year=2025)

        with self.assertRaisesRegex(CommandError, "GIS source year 2024"):
            BrazosAnnualRefresh(cad, gis).run(RefreshOptions(tax_year=2025))

        self.assertFalse(cad.persisted)
        self.assertFalse(gis.persisted)
        self.assertFalse(cad.cleaned)
        self.assertFalse(gis.cleaned)

    def test_dry_run_validates_sources_without_persisting_or_cleaning(self):
        cad = _Stage("cad", source_year=2025, target_year=2025)
        gis = _Stage("gis", source_year=2025, target_year=2025)

        with self.assertRaisesRegex(CommandError, "inspectable PACS"):
            BrazosAnnualRefresh(cad, gis).run(RefreshOptions(dry_run=True))
        self.assertFalse(cad.persisted)
        self.assertFalse(gis.persisted)
        self.assertFalse(cad.cleaned)
        self.assertFalse(gis.cleaned)


class AnnualRefreshPublicationTests(TestCase):
    def test_gis_failure_rolls_back_cad_rebuild_and_retains_extracts(self):
        from counties.brazos.models import PropertyAccount

        PropertyAccount.objects.create(
            prop_id="000000010013",
            tax_year=2025,
            owner_name="Original owner",
            situs_address="100 Original Street",
            assessed_value=Decimal("100000"),
        )
        cad = _ReplacingCadStage("cad", source_year=2025, target_year=2025)
        gis = _FailingGisStage("gis", source_year=2025, target_year=2025)

        with self.assertRaisesRegex(RuntimeError, "GIS enrichment failed"):
            BrazosAnnualRefresh(cad, gis).run(RefreshOptions(tax_year=2025))

        restored = PropertyAccount.objects.get(prop_id="000000010013", tax_year=2025)
        self.assertEqual(restored.owner_name, "Original owner")
        self.assertEqual(restored.situs_address, "100 Original Street")
        self.assertEqual(restored.assessed_value, Decimal("100000"))
        self.assertTrue(cad.persisted)
        self.assertTrue(gis.persisted)
        self.assertFalse(cad.cleaned)
        self.assertFalse(gis.cleaned)


class AnnualRefreshCommandTests(TestCase):
    """Exercise the public command against real PACS and GIS file formats."""

    @staticmethod
    def _line(length: int, fields: dict[tuple[int, int], str]) -> str:
        chars = [" "] * length
        for (start, end), value in fields.items():
            value = value[: end - start]
            chars[start : start + len(value)] = list(value)
        return "".join(chars)

    def _write_cad_source(self, extract_dir: Path) -> None:
        extract_dir.mkdir(parents=True)
        (extract_dir / "APPRAISAL_INFO.TXT").write_text(
            self._line(
                987,
                {
                    (0, 12): "000000010013",
                    (17, 22): "2025",
                    (608, 678): "CAD owner",
                    (753, 873): "1 Certified Way",
                    (873, 923): "Bryan",
                    (923, 925): "TX",
                    (978, 983): "77801",
                },
            )
            + "\n",
            encoding="utf-8",
        )
        (extract_dir / "APPRAISAL_ENTITY_INFO.TXT").write_text(
            self._line(
                418,
                {
                    (0, 12): "000000010013",
                    (12, 17): "2025",
                    (41, 53): "000000237993",
                    (53, 63): "BRZ001",
                    (63, 113): "BRAZOS COUNTY",
                    (148, 163): "000000000250000",
                    (163, 178): "000000000250000",
                },
            )
            + "\n",
            encoding="utf-8",
        )
        files = {
            "APPRAISAL_LAND_DETAIL.TXT": self._line(
                184,
                {
                    (0, 12): "000000010013",
                    (12, 16): "2025",
                },
            ),
            "APPRAISAL_IMPROVEMENT_INFO.TXT": self._line(
                49,
                {
                    (0, 12): "000000010013",
                    (12, 16): "2025",
                    (16, 28): "000000000001",
                },
            ),
            "APPRAISAL_IMPROVEMENT_DETAIL.TXT": self._line(
                622,
                {
                    (0, 12): "000000010013",
                    (12, 16): "2025",
                    (16, 28): "000000000001",
                },
            ),
            "APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT": self._line(
                87,
                {
                    (0, 12): "000000010013",
                    (12, 16): "2025",
                    (16, 28): "000000000001",
                },
            ),
        }
        for filename, contents in files.items():
            (extract_dir / filename).write_text(contents + "\n", encoding="utf-8")

    @staticmethod
    def _write_gis_source(shapefile_path: Path) -> None:
        import geopandas as gpd
        from shapely.geometry import Point

        gpd.GeoDataFrame(
            {
                "PROP_ID": [10013],
                "situs_num": ["5000"],
                "situs_stre": ["SILVER HILL"],
                "situs_st_1": ["RD"],
                "situs_st_2": [""],
                "situs_unit": [""],
                "state_cd": ["E1"],
                "living_are": [1800.0],
                "class_cd": ["RV3"],
                "yr_built": [1980],
                "yr_blt": [1980],
            },
            geometry=[Point(3556000, 10120000)],
            crs="EPSG:2277",
        ).to_file(shapefile_path)

    def _stage_sources(self, root: Path) -> tuple[Path, Path, Path]:
        download_dir = root / "downloads"
        extract_root = root / "extracted"
        download_dir.mkdir()
        (download_dir / "bcad_certified_2025.zip").write_bytes(b"retained archive")

        cad_extract = extract_root / "2025"
        gis_extract = extract_root / "gis" / "2025"
        self._write_cad_source(cad_extract)
        gis_extract.mkdir(parents=True)
        self._write_gis_source(gis_extract / "parcels.shp")
        return download_dir, cad_extract, gis_extract

    def test_command_publishes_cad_and_gis_as_one_complete_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            download_dir, cad_extract, gis_extract = self._stage_sources(root)

            with self.settings(
                BCAD_DOWNLOAD_DIR=str(download_dir),
                BCAD_EXTRACT_DIR=str(root / "extracted"),
            ):
                call_command(
                    "refresh_brazos_annual",
                    "--skip-download",
                    "--skip-extract",
                    "--year",
                    "2025",
                )

            account = PropertyAccount.objects.get(prop_id="000000010013", tax_year=2025)
            self.assertEqual(account.owner_name, "CAD owner")
            self.assertEqual(account.assessed_value, Decimal("250000"))
            self.assertEqual(account.situs_address, "5000 SILVER HILL RD")
            self.assertEqual(account.state_class, "E1")
            self.assertIsNotNone(account.latitude)
            self.assertIsNotNone(account.longitude)
            self.assertEqual(account.coordinate_source_year, 2025)
            snapshot = BrazosPropertySnapshot.objects.get(is_active=True)
            self.assertEqual(snapshot.outcome, SnapshotOutcome.COMPLETED)
            self.assertEqual(snapshot.tax_year, 2025)
            self.assertEqual(snapshot.cad_source_year, 2025)
            self.assertEqual(snapshot.gis_source_year, 2025)
            operation = ImportOperation.objects.latest("started_at")
            working = root / "extracted" / ".imports" / str(operation.pk)
            self.assertFalse((working / "2025").exists())
            self.assertFalse((working / "gis" / "2025").exists())
            self.assertTrue(cad_extract.exists())
            self.assertTrue(gis_extract.exists())

    def test_command_rolls_back_and_retains_both_sources_when_gis_fails(self):
        PropertyAccount.objects.create(
            prop_id="000000010013",
            tax_year=2025,
            owner_name="Prior snapshot",
            situs_address="100 Existing Street",
            assessed_value=Decimal("100000"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            download_dir, cad_extract, gis_extract = self._stage_sources(root)

            with (
                self.settings(
                    BCAD_DOWNLOAD_DIR=str(download_dir),
                    BCAD_EXTRACT_DIR=str(root / "extracted"),
                ),
                patch(
                    "counties.brazos.gis_refresh." "GisRefreshStage.persist",
                    side_effect=RuntimeError("forced GIS failure"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "forced GIS failure"):
                    call_command(
                        "refresh_brazos_annual",
                        "--skip-download",
                        "--skip-extract",
                        "--year",
                        "2025",
                    )

            restored = PropertyAccount.objects.get(prop_id="000000010013", tax_year=2025)
            self.assertEqual(restored.owner_name, "Prior snapshot")
            self.assertEqual(restored.situs_address, "100 Existing Street")
            self.assertEqual(restored.assessed_value, Decimal("100000"))
            self.assertTrue(cad_extract.exists())
            self.assertTrue(gis_extract.exists())
