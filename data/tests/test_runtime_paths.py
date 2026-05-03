import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from taxprotest.runtime_paths import migrate_runtime_artifacts, resolve_runtime_paths


class ResolveRuntimePathsTests(SimpleTestCase):
    def test_defaults_to_var_runtime_directories(self) -> None:
        paths = resolve_runtime_paths("/tmp/project", env={})

        self.assertEqual(paths.download_dir, Path("/tmp/project/var/downloads"))
        self.assertEqual(paths.extract_dir, Path("/tmp/project/var/extracted"))
        self.assertEqual(paths.log_dir, Path("/tmp/project/var/logs"))
        self.assertEqual(paths.report_dir, Path("/tmp/project/var/reports"))

    def test_honors_environment_overrides(self) -> None:
        paths = resolve_runtime_paths(
            "/tmp/project",
            env={
                "HCAD_DOWNLOAD_DIR": "/srv/downloads",
                "HCAD_EXTRACT_DIR": "relative/extracted",
                "HCAD_LOG_DIR": "/srv/logs",
                "PROJECT_REPORT_DIR": "relative/reports",
            },
        )

        self.assertEqual(paths.download_dir, Path("/srv/downloads"))
        self.assertEqual(paths.extract_dir, Path("/tmp/project/relative/extracted"))
        self.assertEqual(paths.log_dir, Path("/srv/logs"))
        self.assertEqual(paths.report_dir, Path("/tmp/project/relative/reports"))


class MigrateRuntimeArtifactsTests(SimpleTestCase):
    def test_moves_legacy_runtime_directories_into_var(self) -> None:
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
            self.assertEqual(
                (root / "var" / "downloads" / "sample.txt").read_text(), "payload"
            )
            self.assertEqual((root / "var" / "logs" / "etl.log").read_text(), "log")

    def test_noops_when_runtime_directories_are_already_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "var" / "downloads").mkdir(parents=True)

            result = migrate_runtime_artifacts(root, env={})

            self.assertFalse(result["moved"])
            self.assertEqual(result["created"], ["var/extracted", "var/logs", "var/reports"])

    def test_preserves_existing_destination_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy_downloads = root / "downloads"
            target_downloads = root / "var" / "downloads"
            legacy_downloads.mkdir()
            target_downloads.mkdir(parents=True)
            (legacy_downloads / "legacy.txt").write_text("old")
            (target_downloads / "existing.txt").write_text("new")

            with patch("taxprotest.runtime_paths.shutil.copy2") as copy2:
                result = migrate_runtime_artifacts(root, env={})

            self.assertTrue(result["moved"])
            self.assertTrue((target_downloads / "legacy.txt").exists())
            self.assertTrue((target_downloads / "existing.txt").exists())
            copy2.assert_not_called()
