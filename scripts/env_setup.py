#!/usr/bin/env python3
"""Create .env and generate or rotate its secrets.

    python3 scripts/env_setup.py                       # create .env, fill missing secrets
    python3 scripts/env_setup.py --rotate              # rotate every generated secret
    python3 scripts/env_setup.py --rotate POSTGRES_PASSWORD
    python3 scripts/env_setup.py -f docker-compose.prod.yml --rotate

Safe to run at any time. Setup leaves real values alone and only fills keys that are
missing, blank or still a ``change-me`` placeholder. Every other line of .env is kept as it
is, and the file is written with mode 0600. Secret values are never printed.

Postgres reads POSTGRES_PASSWORD only when it first creates its data volume, so a new
password is set on the database role (ALTER ROLE) before .env is written, and .env is left
unchanged if that fails. Running app services are then recreated so they pick up the new
values. This runs on the host with only the standard library: it has to work before
docker compose can start, because compose refuses to start without POSTGRES_PASSWORD.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, TextIO

ROOT = Path(__file__).resolve().parents[1]

# token_urlsafe output needs no quoting in a DATABASE_URL and holds no "$" for compose to
# interpolate, unlike Django's get_random_secret_key().
GENERATED_KEYS = ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD")
_INFRASTRUCTURE_SERVICES = frozenset({"postgres", "redis"})
_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


class ComposeError(RuntimeError):
    """A docker compose step failed."""


def generate_secret() -> str:
    return secrets.token_urlsafe(50)


def parse_values(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        match = _LINE.match(line.strip())
        if match:
            values[match["key"]] = match["value"].strip().strip("\"'")
    return values


def _is_placeholder(value: str | None) -> bool:
    return value is None or value == "" or value.startswith("change-me")


def apply_values(text: str, updates: dict[str, str]) -> str:
    """Replace each updated key in place and append the ones .env does not have yet."""
    remaining = dict(updates)
    lines = []
    for line in text.splitlines(keepends=True):
        match = _LINE.match(line.strip())
        if match and match["key"] in remaining:
            ending = "\n" if line.endswith("\n") else ""
            line = f"{match['key']}={remaining.pop(match['key'])}{ending}"
        lines.append(line)
    if remaining:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.extend(f"{key}={value}\n" for key, value in remaining.items())
    return "".join(lines)


class ComposeStack(Protocol):
    def postgres_running(self) -> bool: ...

    def postgres_volume_exists(self) -> bool: ...

    def set_role_password(self, user: str, database: str, password: str) -> None: ...

    def running_app_services(self) -> tuple[str, ...]: ...

    def recreate(self, services: tuple[str, ...]) -> None: ...


class DockerCompose:
    """The project's docker compose stack, driven through the docker CLI."""

    def __init__(self, compose_files: Sequence[str], env_file: Path) -> None:
        self._base = ["docker", "compose", "--env-file", str(env_file)]
        for compose_file in compose_files:
            self._base += ["-f", compose_file]

    def _run(self, *args: str, stdin: str | None = None) -> str:
        try:
            result = subprocess.run(
                [*self._base, *args],
                input=stdin,
                capture_output=True,
                text=True,
                cwd=ROOT,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ComposeError("docker is not installed") from exc
        if result.returncode != 0:
            raise ComposeError(result.stderr.strip() or f"docker compose {args[0]} failed")
        return result.stdout

    def postgres_running(self) -> bool:
        try:
            return bool(self._run("ps", "--status", "running", "-q", "postgres").strip())
        except ComposeError:
            return False

    def postgres_volume_exists(self) -> bool:
        try:
            project = json.loads(self._run("config", "--format", "json"))["name"]
            listed = subprocess.run(
                [
                    "docker",
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                    "--filter",
                    "label=com.docker.compose.volume=pgdata",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except (ComposeError, OSError, subprocess.CalledProcessError, KeyError, ValueError):
            return False
        return bool(listed.stdout.strip())

    def set_role_password(self, user: str, database: str, password: str) -> None:
        # The SQL goes in on stdin so the password never appears in a process list.
        role = user.replace('"', '""')
        self._run(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-q",
            "-U",
            user,
            "-d",
            database,
            stdin=f"ALTER ROLE \"{role}\" PASSWORD '{password}';\n",
        )

    def running_app_services(self) -> tuple[str, ...]:
        try:
            listed = self._run("ps", "--status", "running", "--services")
        except ComposeError:
            return ()
        return tuple(
            service for service in listed.split() if service not in _INFRASTRUCTURE_SERVICES
        )

    def recreate(self, services: tuple[str, ...]) -> None:
        self._run("up", "-d", "--no-deps", *services)


def _write_private(path: Path, text: str) -> Path:
    """Write text beside path with mode 0600 and return the temporary file."""
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    os.chmod(name, 0o600)
    return Path(name)


def main(
    argv: Sequence[str] | None = None,
    *,
    compose: ComposeStack | None = None,
    out: TextIO = sys.stdout,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--rotate",
        nargs="*",
        choices=GENERATED_KEYS,
        metavar="KEY",
        help=f"Generate new values (default: all of {', '.join(GENERATED_KEYS)})",
    )
    parser.add_argument(
        "-f",
        "--compose-file",
        action="append",
        default=[],
        help="Compose file of the stack to update (repeatable; default: docker-compose.yml)",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Print the command that recreates the app services instead of running it",
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help=argparse.SUPPRESS)
    parser.add_argument(
        "--example", type=Path, default=ROOT / ".env.example", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    env_path: Path = args.env_file
    stack = compose or DockerCompose(args.compose_file, env_path)
    compose_command = "docker compose" + "".join(f" -f {name}" for name in args.compose_file)

    created = not env_path.exists()
    text = args.example.read_text() if created else env_path.read_text()
    values = parse_values(text)
    rotating = set(GENERATED_KEYS if args.rotate == [] else args.rotate or ())
    updates = {
        key: generate_secret()
        for key in GENERATED_KEYS
        if key in rotating or _is_placeholder(values.get(key))
    }

    if not updates:
        if not created:
            os.chmod(env_path, 0o600)
        print(f"{env_path.name} is set up; nothing to generate.", file=out)
        return 0

    temporary = _write_private(env_path, apply_values(text, updates))
    try:
        if "POSTGRES_PASSWORD" in updates:
            if stack.postgres_running():
                stack.set_role_password(
                    values.get("POSTGRES_USER") or "taxprotest",
                    values.get("POSTGRES_DB") or "taxprotest",
                    updates["POSTGRES_PASSWORD"],
                )
            elif stack.postgres_volume_exists():
                print(
                    "The database already has data, so its password can only be changed "
                    f"while it runs. Start it with `{compose_command} up -d postgres` and run "
                    "this again. .env is unchanged.",
                    file=out,
                )
                temporary.unlink()
                return 1
        os.replace(temporary, env_path)
    except ComposeError as exc:
        temporary.unlink()
        print(f"Could not set the database password: {exc}. .env is unchanged.", file=out)
        return 1

    verb = "Created" if created else "Updated"
    print(f"{verb} {env_path.name}: generated {', '.join(updates)}.", file=out)
    if "DJANGO_SECRET_KEY" in updates and not created:
        print("A new DJANGO_SECRET_KEY signs everyone out of the site.", file=out)
    if "REDIS_PASSWORD" in values:
        print("REDIS_PASSWORD is not used by any service, so it was left alone.", file=out)

    services = stack.running_app_services()
    if services:
        recreate_command = f"{compose_command} up -d --no-deps {' '.join(services)}"
        if args.no_restart:
            print(f"Recreate the running services to use it: {recreate_command}", file=out)
        else:
            try:
                stack.recreate(services)
            except ComposeError as exc:
                print(f"Could not recreate {', '.join(services)}: {exc}", file=out)
                print(f"Run: {recreate_command}", file=out)
                return 1
            print(f"Recreated {', '.join(services)}.", file=out)
    print(
        "The Django admin password lives in the database, not .env. Change it with "
        f"`{compose_command} exec web python manage.py changepassword <username>`.",
        file=out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
