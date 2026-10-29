"""Contract tests for scripts/env_setup.py, the .env setup and secret-rotation command."""

from __future__ import annotations

import importlib.util
import stat
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "env_setup.py"
_spec = importlib.util.spec_from_file_location("env_setup", _SCRIPT)
assert _spec is not None and _spec.loader is not None
env_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(env_setup)

EXAMPLE = (
    "# Required\n"
    "DJANGO_SECRET_KEY=change-me\n"
    "POSTGRES_PASSWORD=change-me\n"
    "POSTGRES_USER=taxprotest\n"
    "POSTGRES_DB=taxprotest\n"
    "DEBUG=0\n"
)


class FakeCompose:
    def __init__(
        self,
        *,
        postgres_running: bool = False,
        postgres_volume: bool = False,
        alter_fails: bool = False,
        app_services: tuple[str, ...] = (),
    ) -> None:
        self._running = postgres_running
        self._volume = postgres_volume
        self._alter_fails = alter_fails
        self._app_services = app_services
        self.role_passwords: list[tuple[str, str, str]] = []
        self.recreated: list[tuple[str, ...]] = []

    def postgres_running(self) -> bool:
        return self._running

    def postgres_volume_exists(self) -> bool:
        return self._volume

    def set_role_password(self, user: str, database: str, password: str) -> None:
        if self._alter_fails:
            raise env_setup.ComposeError("ALTER ROLE failed")
        self.role_passwords.append((user, database, password))

    def running_app_services(self) -> tuple[str, ...]:
        return self._app_services

    def recreate(self, services: tuple[str, ...]) -> None:
        self.recreated.append(services)


class EnvSetupTests(SimpleTestCase):
    def _run(self, directory: str, compose: FakeCompose, *args: str) -> tuple[int, str]:
        out = StringIO()
        code = env_setup.main(
            [
                "--env-file",
                str(Path(directory) / ".env"),
                "--example",
                str(Path(directory) / ".env.example"),
                *args,
            ],
            compose=compose,
            out=out,
        )
        return code, out.getvalue()

    def _write(self, directory: str, name: str, text: str) -> Path:
        path = Path(directory) / name
        path.write_text(text)
        return path

    def test_creates_env_from_example_and_fills_placeholders(self) -> None:
        with TemporaryDirectory() as directory:
            self._write(directory, ".env.example", EXAMPLE)
            code, output = self._run(directory, FakeCompose())
            env_path = Path(directory) / ".env"
            values = env_setup.parse_values(env_path.read_text())
            mode = stat.S_IMODE(env_path.stat().st_mode)
            text = env_path.read_text()

        self.assertEqual(code, 0)
        self.assertIn("# Required\n", text)
        self.assertEqual(values["POSTGRES_USER"], "taxprotest")
        for key in ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD"):
            self.assertNotEqual(values[key], "change-me")
            self.assertGreaterEqual(len(values[key]), 40)
            self.assertNotIn(values[key], output)
        self.assertEqual(mode, 0o600)

    def test_setup_keeps_real_values_and_unrelated_lines(self) -> None:
        existing = (
            "DJANGO_SECRET_KEY=kept-secret\n"
            "# local dev\n"
            "DEBUG=1\n"
            "POSTGRES_PASSWORD=kept-password\n"
        )
        with TemporaryDirectory() as directory:
            self._write(directory, ".env.example", EXAMPLE)
            env_path = self._write(directory, ".env", existing)
            env_path.chmod(0o755)
            code, _ = self._run(directory, FakeCompose(postgres_running=True))
            text = env_path.read_text()
            mode = stat.S_IMODE(env_path.stat().st_mode)

        self.assertEqual(code, 0)
        self.assertEqual(text, existing)
        self.assertEqual(mode, 0o600)

    def test_missing_generated_keys_are_appended(self) -> None:
        with TemporaryDirectory() as directory:
            self._write(directory, ".env.example", EXAMPLE)
            env_path = self._write(directory, ".env", "DEBUG=1\nPOSTGRES_PASSWORD=kept\n")
            code, _ = self._run(directory, FakeCompose())
            values = env_setup.parse_values(env_path.read_text())

        self.assertEqual(code, 0)
        self.assertEqual(values["POSTGRES_PASSWORD"], "kept")
        self.assertEqual(values["DEBUG"], "1")
        self.assertGreaterEqual(len(values["DJANGO_SECRET_KEY"]), 40)

    def test_generated_values_are_safe_in_a_database_url_and_compose(self) -> None:
        for _ in range(200):
            value = env_setup.generate_secret()
            self.assertRegex(value, r"^[A-Za-z0-9_-]+$")

    def test_rotation_updates_the_database_role_before_writing_and_recreates_services(
        self,
    ) -> None:
        compose = FakeCompose(
            postgres_running=True, postgres_volume=True, app_services=("web", "worker")
        )
        with TemporaryDirectory() as directory:
            self._write(directory, ".env.example", EXAMPLE)
            env_path = self._write(
                directory,
                ".env",
                "DJANGO_SECRET_KEY=old-key\nPOSTGRES_PASSWORD=old-pw\nPOSTGRES_USER=tp\n"
                "POSTGRES_DB=tpdb\n",
            )
            code, output = self._run(directory, compose, "--rotate")
            values = env_setup.parse_values(env_path.read_text())

        self.assertEqual(code, 0)
        self.assertNotEqual(values["DJANGO_SECRET_KEY"], "old-key")
        self.assertNotEqual(values["POSTGRES_PASSWORD"], "old-pw")
        self.assertEqual(compose.role_passwords, [("tp", "tpdb", values["POSTGRES_PASSWORD"])])
        self.assertEqual(compose.recreated, [("web", "worker")])
        self.assertNotIn(values["POSTGRES_PASSWORD"], output)
        self.assertNotIn(values["DJANGO_SECRET_KEY"], output)

    def test_rotating_one_key_leaves_the_other_alone(self) -> None:
        compose = FakeCompose(postgres_running=True, postgres_volume=True)
        with TemporaryDirectory() as directory:
            self._write(directory, ".env.example", EXAMPLE)
            env_path = self._write(
                directory, ".env", "DJANGO_SECRET_KEY=old-key\nPOSTGRES_PASSWORD=old-pw\n"
            )
            code, _ = self._run(directory, compose, "--rotate", "DJANGO_SECRET_KEY")
            values = env_setup.parse_values(env_path.read_text())

        self.assertEqual(code, 0)
        self.assertEqual(values["POSTGRES_PASSWORD"], "old-pw")
        self.assertNotEqual(values["DJANGO_SECRET_KEY"], "old-key")
        self.assertEqual(compose.role_passwords, [])

    def test_failed_role_update_leaves_env_unchanged(self) -> None:
        original = "DJANGO_SECRET_KEY=old-key\nPOSTGRES_PASSWORD=old-pw\n"
        compose = FakeCompose(
            postgres_running=True, postgres_volume=True, alter_fails=True, app_services=("web",)
        )
        with TemporaryDirectory() as directory:
            self._write(directory, ".env.example", EXAMPLE)
            env_path = self._write(directory, ".env", original)
            code, output = self._run(directory, compose, "--rotate")
            text = env_path.read_text()
            leftovers = sorted(p.name for p in Path(directory).iterdir())

        self.assertEqual(code, 1)
        self.assertEqual(text, original)
        self.assertEqual(compose.recreated, [])
        self.assertEqual(leftovers, [".env", ".env.example"])
        self.assertIn("ALTER ROLE failed", output)

    def test_refuses_to_change_the_database_password_while_its_data_is_offline(self) -> None:
        original = "DJANGO_SECRET_KEY=old-key\nPOSTGRES_PASSWORD=old-pw\n"
        compose = FakeCompose(postgres_running=False, postgres_volume=True)
        with TemporaryDirectory() as directory:
            self._write(directory, ".env.example", EXAMPLE)
            env_path = self._write(directory, ".env", original)
            code, output = self._run(directory, compose, "--rotate")
            text = env_path.read_text()

        self.assertEqual(code, 1)
        self.assertEqual(text, original)
        self.assertIn("docker compose up -d postgres", output)

    def test_no_restart_prints_the_command_instead(self) -> None:
        compose = FakeCompose(postgres_running=True, postgres_volume=True, app_services=("web",))
        with TemporaryDirectory() as directory:
            self._write(directory, ".env.example", EXAMPLE)
            self._write(directory, ".env", "DJANGO_SECRET_KEY=old\nPOSTGRES_PASSWORD=old\n")
            code, output = self._run(
                directory, compose, "--rotate", "DJANGO_SECRET_KEY", "--no-restart"
            )

        self.assertEqual(code, 0)
        self.assertEqual(compose.recreated, [])
        self.assertIn("docker compose up -d --no-deps web", output)

    def test_unknown_rotation_key_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            self._write(directory, ".env.example", EXAMPLE)
            with self.assertRaises(SystemExit):
                self._run(directory, FakeCompose(), "--rotate", "REDIS_PASSWORD")
