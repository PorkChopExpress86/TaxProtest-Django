"""Resolution of the on-disk runtime directories each county app reads and writes.

Downloaded archives, extracted source files, ETL logs, and generated reports all
live *inside the county app that owns them* — ``counties/<slug>/var/`` — rather
than in a single shared tree at the project root. Each county's ETL differs, so
each county owns its own staging area; nothing about one county's data layout
constrains another's.

Every directory can be overridden with an environment variable so containers and
production hosts can point the (large) staging area at a mounted volume:

    <PREFIX>_DOWNLOAD_DIR   <PREFIX>_EXTRACT_DIR
    <PREFIX>_LOG_DIR        <PREFIX>_REPORT_DIR

where ``<PREFIX>`` is the county's appraisal-district abbreviation (``HCAD`` for
Harris, ``BCAD`` for Brazos). Relative values resolve against the project root.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

#: Directory name, inside each county app package, holding that county's runtime data.
COUNTY_RUNTIME_DIRNAME = "var"

#: Package directory holding the county apps.
COUNTIES_DIRNAME = "counties"

#: Legacy alias kept so ``PROJECT_REPORT_DIR=...`` deployments keep working.
LEGACY_REPORT_DIR_ENV = "PROJECT_REPORT_DIR"


@dataclass(frozen=True)
class CountyRuntimeSpec:
    """Static description of one county's runtime directory conventions."""

    slug: str
    package_dirname: str
    env_prefix: str


#: Registered counties, in the order they should be reported to operators.
COUNTY_RUNTIME_SPECS: tuple[CountyRuntimeSpec, ...] = (
    CountyRuntimeSpec(slug="harris", package_dirname="harris", env_prefix="HCAD"),
    CountyRuntimeSpec(slug="brazos", package_dirname="brazos", env_prefix="BCAD"),
)


@dataclass(frozen=True)
class CountyRuntimePaths:
    """Resolved runtime directories for a single county."""

    slug: str
    root: Path
    download_dir: Path
    extract_dir: Path
    log_dir: Path
    report_dir: Path

    def all_dirs(self) -> tuple[Path, ...]:
        return (self.download_dir, self.extract_dir, self.log_dir, self.report_dir)

    def ensure(self) -> None:
        for directory in self.all_dirs():
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class RuntimePaths:
    """Runtime directories for every registered county, keyed by slug."""

    counties: Mapping[str, CountyRuntimePaths]

    def __getitem__(self, slug: str) -> CountyRuntimePaths:
        return self.counties[slug]

    def __iter__(self) -> Iterator[CountyRuntimePaths]:
        return iter(self.counties.values())

    @property
    def harris(self) -> CountyRuntimePaths:
        return self.counties["harris"]

    @property
    def brazos(self) -> CountyRuntimePaths:
        return self.counties["brazos"]


def _resolve_env_path(
    base_dir: Path, env: Mapping[str, str], names: tuple[str, ...], default: Path
) -> Path:
    """First non-empty env var in ``names`` wins; relative values hang off ``base_dir``."""
    for name in names:
        value = env.get(name)
        if not value:
            continue
        candidate = Path(value)
        return candidate if candidate.is_absolute() else base_dir / candidate
    return default


def county_runtime_root(base_dir: str | os.PathLike[str], spec: CountyRuntimeSpec) -> Path:
    """Default runtime root for a county: ``<base>/counties/<pkg>/var``."""
    return Path(base_dir) / COUNTIES_DIRNAME / spec.package_dirname / COUNTY_RUNTIME_DIRNAME


def resolve_county_runtime_paths(
    base_dir: str | os.PathLike[str],
    spec: CountyRuntimeSpec,
    env: Mapping[str, str] | None = None,
) -> CountyRuntimePaths:
    root = Path(base_dir)
    environ = env if env is not None else os.environ
    default_root = county_runtime_root(root, spec)

    report_env_names = (f"{spec.env_prefix}_REPORT_DIR",)
    if spec.slug == "harris":
        # Harris predates the per-county layout and shipped with this name.
        report_env_names += (LEGACY_REPORT_DIR_ENV,)

    return CountyRuntimePaths(
        slug=spec.slug,
        root=default_root,
        download_dir=_resolve_env_path(
            root, environ, (f"{spec.env_prefix}_DOWNLOAD_DIR",), default_root / "downloads"
        ),
        extract_dir=_resolve_env_path(
            root, environ, (f"{spec.env_prefix}_EXTRACT_DIR",), default_root / "extracted"
        ),
        log_dir=_resolve_env_path(
            root, environ, (f"{spec.env_prefix}_LOG_DIR",), default_root / "logs"
        ),
        report_dir=_resolve_env_path(root, environ, report_env_names, default_root / "reports"),
    )


def resolve_runtime_paths(
    base_dir: str | os.PathLike[str], env: Mapping[str, str] | None = None
) -> RuntimePaths:
    return RuntimePaths(
        counties={
            spec.slug: resolve_county_runtime_paths(base_dir, spec, env=env)
            for spec in COUNTY_RUNTIME_SPECS
        }
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


def legacy_runtime_locations(base_dir: str | os.PathLike[str]) -> dict[Path, tuple[str, str]]:
    """Map every pre-``counties/`` runtime directory to its ``(slug, kind)`` destination.

    Covers both the original project-root layout (``downloads/``, ``extracted/``)
    and the intermediate shared ``var/`` tree, including the Brazos staging dirs
    that briefly lived under the Harris app as ``data/cad_downloads``.
    """
    root = Path(base_dir)
    return {
        # Original project-root layout.
        root / "downloads": ("harris", "download_dir"),
        root / "extracted": ("harris", "extract_dir"),
        root / "logs": ("harris", "log_dir"),
        root / "reports": ("harris", "report_dir"),
        # Shared var/ tree.
        root / "var" / "downloads": ("harris", "download_dir"),
        root / "var" / "extracted": ("harris", "extract_dir"),
        root / "var" / "logs": ("harris", "log_dir"),
        root / "var" / "reports": ("harris", "report_dir"),
        root / "var" / "bcad_downloads": ("brazos", "download_dir"),
        root / "var" / "bcad_extracted": ("brazos", "extract_dir"),
        # Brazos downloads that were staged inside the Harris app package.
        root / "data" / "cad_downloads": ("brazos", "download_dir"),
        root / "counties" / "harris" / "cad_downloads": ("brazos", "download_dir"),
    }


def migrate_runtime_artifacts(
    base_dir: str | os.PathLike[str], env: Mapping[str, str] | None = None
) -> dict[str, object]:
    """Move any legacy runtime directories into the per-county ``var/`` trees.

    Idempotent: directories already in place are left alone, and files that
    already exist at the destination win over the legacy copy.
    """
    root = Path(base_dir)
    paths = resolve_runtime_paths(root, env=env)

    created: list[str] = []
    moved: list[str] = []

    for county in paths:
        for target in county.all_dirs():
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
                created.append(_display(root, target))

    for legacy, (slug, kind) in legacy_runtime_locations(root).items():
        target = getattr(paths[slug], kind)
        if legacy == target or not legacy.exists():
            continue

        if legacy.is_file():
            if target.exists():
                legacy.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(legacy), str(target))
            moved.append(_display(root, legacy))
            continue

        _merge_tree(legacy, target)
        legacy.rmdir()
        moved.append(_display(root, legacy))

    return {"created": created, "moved": moved}


def _display(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
