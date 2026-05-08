from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

RUNTIME_ROOT_DIRNAME = "var"


@dataclass(frozen=True)
class RuntimePaths:
    download_dir: Path
    extract_dir: Path
    log_dir: Path
    report_dir: Path


def _resolve_env_path(
    base_dir: Path, env: Mapping[str, str], name: str, default_relative: str
) -> Path:
    value = env.get(name)
    if not value:
        return base_dir / default_relative

    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return base_dir / candidate


def resolve_runtime_paths(
    base_dir: str | os.PathLike[str], env: Mapping[str, str] | None = None
) -> RuntimePaths:
    root = Path(base_dir)
    environ = env if env is not None else os.environ
    runtime_root = Path(RUNTIME_ROOT_DIRNAME)
    return RuntimePaths(
        download_dir=_resolve_env_path(
            root, environ, "HCAD_DOWNLOAD_DIR", str(runtime_root / "downloads")
        ),
        extract_dir=_resolve_env_path(
            root, environ, "HCAD_EXTRACT_DIR", str(runtime_root / "extracted")
        ),
        log_dir=_resolve_env_path(root, environ, "HCAD_LOG_DIR", str(runtime_root / "logs")),
        report_dir=_resolve_env_path(
            root, environ, "PROJECT_REPORT_DIR", str(runtime_root / "reports")
        ),
    )


def resolve_from_base(base_dir: str | os.PathLike[str], value: str | os.PathLike[str]) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return Path(base_dir) / candidate


def _merge_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_dir():
            _merge_tree(entry, target)
            entry.rmdir()
            continue
        if target.exists():
            entry.unlink()
            continue
        shutil.move(str(entry), str(target))


def migrate_runtime_artifacts(
    base_dir: str | os.PathLike[str], env: Mapping[str, str] | None = None
) -> dict[str, object]:
    root = Path(base_dir)
    paths = resolve_runtime_paths(root, env=env)
    legacy_to_target = {
        root / "downloads": paths.download_dir,
        root / "extracted": paths.extract_dir,
        root / "logs": paths.log_dir,
        root / "reports": paths.report_dir,
    }

    created: list[str] = []
    moved: list[str] = []

    for target in legacy_to_target.values():
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
            created.append(str(target.relative_to(root)))

    for legacy, target in legacy_to_target.items():
        if legacy == target or not legacy.exists():
            continue

        if legacy.is_file():
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(legacy), str(target))
            else:
                legacy.unlink()
            moved.append(str(legacy.relative_to(root)))
            continue

        _merge_tree(legacy, target)
        legacy.rmdir()
        moved.append(str(legacy.relative_to(root)))

    return {
        "created": created,
        "moved": moved,
    }
