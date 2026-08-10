import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from taxprotest.runtime_paths import migrate_runtime_artifacts, resolve_runtime_paths


class ResolveRuntimePathsTests(SimpleTestCase):
    def test_defaults_to_per_county_var_directories(self) -> None:
        paths = resolve_runtime_paths("/tmp/project", env={})

        harris = paths["harris"]
        self.assertEqual(harris.download_dir, Path("/tmp/project/counties/harris/var/downloads"))
        self.assertEqual(harris.extract_dir, Path("/tmp/project/counties/harris/var/extracted"))
        self.assertEqual(harris.log_dir, Path("/tmp/project/counties/harris/var/logs"))
        self.assertEqual(harris.report_dir, Path("/tmp/project/counties/harris/var/reports"))

        brazos = paths["brazos"]
        self.assertEqual(brazos.download_dir, Path("/tmp/project/counties/brazos/var/downloads"))
        self.assertEqual(brazos.extract_dir, Path("/tmp/project/counties/brazos/var/extracted"))
        self.assertEqual(brazos.log_dir, Path("/tmp/project/counties/brazos/var/logs"))
        self.assertEqual(brazos.report_dir, Path("/tmp/project/counties/brazos/var/reports"))

    def test_counties_do_not_share_a_staging_directory(self) -> None:
        paths = resolve_runtime_paths("/tmp/project", env={})

        harris_dirs = set(paths["harris"].all_dirs())
        brazos_dirs = set(paths["brazos"].all_dirs())

        self.assertEqual(harris_dirs & brazos_dirs, set())

    def test_honors_environment_overrides(self) -> None:
        paths = resolve_runtime_paths(
            "/tmp/project",
            env={
                "HCAD_DOWNLOAD_DIR": "/srv/downloads",
                "HCAD_EXTRACT_DIR": "relative/extracted",
                "HCAD_LOG_DIR": "/srv/logs",
                "HCAD_REPORT_DIR": "relative/reports",
                "BCAD_DOWNLOAD_DIR": "/srv/bcad",
                "BCAD_EXTRACT_DIR": "relative/bcad-extracted",
            },
        )

        harris = paths["harris"]
        self.assertEqual(harris.download_dir, Path("/srv/downloads"))
        self.assertEqual(harris.extract_dir, Path("/tmp/project/relative/extracted"))
        self.assertEqual(harris.log_dir, Path("/srv/logs"))
        self.assertEqual(harris.report_dir, Path("/tmp/project/relative/reports"))

        brazos = paths["brazos"]
        self.assertEqual(brazos.download_dir, Path("/srv/bcad"))
        self.assertEqual(brazos.extract_dir, Path("/tmp/project/relative/bcad-extracted"))
        # Unset overrides keep the in-app default.
        self.assertEqual(brazos.log_dir, Path("/tmp/project/counties/brazos/var/logs"))

    def test_legacy_project_report_dir_still_maps_to_harris(self) -> None:
        paths = resolve_runtime_paths("/tmp/project", env={"PROJECT_REPORT_DIR": "/srv/reports"})

        self.assertEqual(paths["harris"].report_dir, Path("/srv/reports"))
        # The legacy name was Harris-only and must not leak into other counties.
        self.assertEqual(
            paths["brazos"].report_dir, Path("/tmp/project/counties/brazos/var/reports")
        )

    def test_county_specific_report_env_wins_over_legacy_alias(self) -> None:
        paths = resolve_runtime_paths(
            "/tmp/project",
            env={"HCAD_REPORT_DIR": "/srv/hcad", "PROJECT_REPORT_DIR": "/srv/legacy"},
        )

        self.assertEqual(paths["harris"].report_dir, Path("/srv/hcad"))


class MigrateRuntimeArtifactsTests(SimpleTestCase):
    def test_moves_project_root_directories_into_the_harris_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy_downloads = root / "downloads"
            legacy_logs = root / "logs"
            legacy_downloads.mkdir()
            legacy_logs.mkdir()
            (legacy_downloads / "sample.txt").write_text("payload")
            (legacy_logs / "etl.log").write_text("log")

            result = migrate_runtime_artifacts(root, env={})

            self.assertTrue(result["moved"])
            self.assertFalse(legacy_downloads.exists())
            self.assertFalse(legacy_logs.exists())
            harris_var = root / "counties" / "harris" / "var"
            self.assertEqual((harris_var / "downloads" / "sample.txt").read_text(), "payload")
            self.assertEqual((harris_var / "logs" / "etl.log").read_text(), "log")

    def test_moves_shared_var_tree_into_the_owning_county(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "var" / "extracted").mkdir(parents=True)
            (root / "var" / "extracted" / "real_acct.txt").write_text("harris")
            (root / "var" / "bcad_extracted" / "2025").mkdir(parents=True)
            (root / "var" / "bcad_extracted" / "2025" / "APPRAISAL_INFO.TXT").write_text("brazos")

            migrate_runtime_artifacts(root, env={})

            self.assertEqual(
                (root / "counties/harris/var/extracted/real_acct.txt").read_text(), "harris"
            )
            self.assertEqual(
                (root / "counties/brazos/var/extracted/2025/APPRAISAL_INFO.TXT").read_text(),
                "brazos",
            )

    def test_moves_brazos_downloads_out_of_the_harris_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stray = root / "counties" / "harris" / "cad_downloads"
            stray.mkdir(parents=True)
            (stray / "bcad_certified_2025.zip").write_bytes(b"zip")

            migrate_runtime_artifacts(root, env={})

            self.assertFalse(stray.exists())
            self.assertEqual(
                (root / "counties/brazos/var/downloads/bcad_certified_2025.zip").read_bytes(),
                b"zip",
            )

    def test_noops_when_runtime_directories_are_already_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            resolve_runtime_paths(root, env={})["harris"].ensure()
            resolve_runtime_paths(root, env={})["brazos"].ensure()

            result = migrate_runtime_artifacts(root, env={})

            self.assertFalse(result["moved"])
            self.assertEqual(result["created"], [])

    def test_preserves_existing_destination_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy_downloads = root / "downloads"
            target_downloads = root / "counties" / "harris" / "var" / "downloads"
            legacy_downloads.mkdir()
            target_downloads.mkdir(parents=True)
            (legacy_downloads / "legacy.txt").write_text("old")
            (target_downloads / "existing.txt").write_text("new")
            (legacy_downloads / "existing.txt").write_text("stale")

            result = migrate_runtime_artifacts(root, env={})

            self.assertTrue(result["moved"])
            self.assertTrue((target_downloads / "legacy.txt").exists())
            self.assertEqual((target_downloads / "existing.txt").read_text(), "new")
