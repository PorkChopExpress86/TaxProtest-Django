"""County registrations: the facts shared Import operation code needs from each county.

Each county makes one registration from its ``AppConfig.ready()``: its unchanged
county candidate port (ADR-0018) plus the facts shared code cannot derive, namely
its writer lock key, its warning logger, and a reader for its managed source roots
(ADR-0022). A registration declares facts and pure readers only; it never holds
sources, parsing, readiness, or a runner.

This is a leaf module: at runtime it imports only the standard library, so every
shared Import module can import it without a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from counties.common.candidate_ports import CandidatePort

# County columns hold at most 16 characters; candidate schemas are ``<slug>_candidate_<hex>``.
_SLUG = re.compile(r"[a-z_]{1,16}")
# Advisory locks are matched in pg_locks as classid 0 and objid key, so keys stay below 2**31.
_LOCK_KEY_LIMIT = 2**31
_PORT_QUESTIONS = ("published", "outcomes", "replay")


class UnknownCounty(LookupError):
    """No county registration exists for the slug."""


class InvalidRegistration(ValueError):
    """A county registration is malformed; raised at startup, never mid-import."""


@dataclass(frozen=True)
class CountyRegistration:
    slug: str
    port: CandidatePort
    writer_lock_key: int  # declared and frozen, never derived from the slug
    warning_logger: str
    source_roots: Callable[[], tuple[Path, ...]]  # read from settings on every call

    def __post_init__(self) -> None:
        if not isinstance(self.slug, str) or not _SLUG.fullmatch(self.slug):
            raise InvalidRegistration(f"County slug must match {_SLUG.pattern}: {self.slug!r}")
        tables = getattr(self.port, "tables", None)
        if not isinstance(getattr(tables, "models", None), tuple) or not all(
            callable(getattr(self.port, name, None)) for name in _PORT_QUESTIONS
        ):
            raise InvalidRegistration(
                f"{self.slug}: port must declare tables and answer {_PORT_QUESTIONS}"
            )
        key = self.writer_lock_key
        if isinstance(key, bool) or not isinstance(key, int) or not 0 < key < _LOCK_KEY_LIMIT:
            raise InvalidRegistration(f"{self.slug}: writer lock key must be an int in (0, 2**31)")
        if not isinstance(self.warning_logger, str) or not self.warning_logger:
            raise InvalidRegistration(f"{self.slug}: warning logger must be a logger name")
        if not callable(self.source_roots):
            raise InvalidRegistration(f"{self.slug}: source roots must be a reader")


_REGISTRATIONS: dict[str, CountyRegistration] = {}


def register(registration: CountyRegistration) -> None:
    """Record ``registration``; called from the county's ``AppConfig.ready()``."""
    if not isinstance(registration, CountyRegistration):
        raise InvalidRegistration(f"Not a county registration: {registration!r}")
    for other in _REGISTRATIONS.values():
        if (
            other.slug != registration.slug
            and other.writer_lock_key == registration.writer_lock_key
        ):
            raise InvalidRegistration(
                f"{registration.slug}: writer lock key {registration.writer_lock_key} "
                f"is already {other.slug}'s"
            )
    _REGISTRATIONS[registration.slug] = registration


def registration_for(slug: str) -> CountyRegistration:
    try:
        return _REGISTRATIONS[slug]
    except KeyError:
        raise UnknownCounty(f"Unknown county: {slug}") from None


def registered_slugs() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRATIONS))


@contextmanager
def isolated_registrations(*, empty: bool = False) -> Iterator[None]:
    """Test hook: changes made inside are undone on exit. Not thread-safe."""
    saved = dict(_REGISTRATIONS)
    if empty:
        _REGISTRATIONS.clear()
    try:
        yield
    finally:
        _REGISTRATIONS.clear()
        _REGISTRATIONS.update(saved)
