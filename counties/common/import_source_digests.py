"""Source file content digests, read at most once per Import operation."""

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

PROVENANCE_FILENAME = ".source-provenance.json"
"""Records what produced the directory holding it; it describes sources and is never one."""

# Device, inode, size, and modification and change times: any write to a file changes
# at least one of these, so a digest is reused only for the bytes it was read from.
_StatKey = tuple[int, int, int, int, int]
_DIGESTS: ContextVar[dict[_StatKey, str] | None] = ContextVar("source_digests", default=None)


def _stat_key(path: Path) -> _StatKey:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def digest_once() -> Iterator[None]:
    """Reuse each digest read inside the block for as long as its file is unchanged."""
    token = _DIGESTS.set({})
    try:
        yield
    finally:
        _DIGESTS.reset(token)


def file_digest(path: Path) -> str:
    digests = _DIGESTS.get()
    if digests is None:
        return _hash(path)
    before = _stat_key(path)
    if before in digests:
        return digests[before]
    digest = _hash(path)
    if _stat_key(path) == before:  # a file written while it was read is read again next time
        digests[before] = digest
    return digest


def is_source_file(path: Path) -> bool:
    # A provenance file, or a copy of one left part written, describes sources.
    return path.is_file() and not path.name.startswith(PROVENANCE_FILENAME)


def _files(root: Path) -> Iterator[Path]:
    """Yield the files under ``root``, leaving out the Import operations' working roots."""
    for directory, children, files in os.walk(root):
        children[:] = [name for name in children if name != ".imports"]
        for filename in sorted(files):
            yield Path(directory) / filename


def tree_digests(root: Path) -> dict[str, str]:
    """Digest every source file under ``root``, keyed by its path relative to ``root``."""
    return {
        str(source.relative_to(root)): file_digest(source)
        for source in _files(root)
        if is_source_file(source)
    }


def carry_digests(source_root: Path, target_root: Path) -> None:
    """Give a completed copy of ``source_root`` the digests already read from its files.

    A source file changed since its digest was read has no digest to give, so its copy
    is read when its digest is next needed.
    """
    digests = _DIGESTS.get()
    if digests is None:
        return
    for source in _files(source_root):
        digest = digests.get(_stat_key(source))
        target = target_root / source.relative_to(source_root)
        if digest is not None and target.is_file():
            digests[_stat_key(target)] = digest
