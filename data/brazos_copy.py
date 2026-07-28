"""PostgreSQL COPY plumbing shared by the Brazos CAD loaders.

Both the certified-roll loader and the GIS loader push rows through
``COPY ... FROM STDIN`` rather than the ORM. The pieces live here so there is one
implementation of the text-format encoding and the generator-backed stream.
"""

from __future__ import annotations

import io
from collections.abc import Iterator

# Bytes requested per read from the COPY stream.
CHUNK_BYTES = 1024 * 1024

# PostgreSQL COPY text format: these four characters carry meaning in the wire
# protocol and must be escaped or they corrupt the record framing.
_COPY_ESCAPES = str.maketrans(
    {
        "\\": "\\\\",
        "\t": "\\t",
        "\n": "\\n",
        "\r": "\\r",
    }
)


def encode_copy_row(values: list[str | None]) -> str:
    """Render one row in PostgreSQL's COPY text format (tab-delimited, \\N = NULL)."""
    return "\t".join("\\N" if v is None else v.translate(_COPY_ESCAPES) for v in values) + "\n"


class CopyStream(io.RawIOBase):
    """A read-only file object that pulls rows from an iterator on demand.

    ``copy_expert`` reads in fixed-size blocks, so only the bytes for the current
    block are ever held. This is what keeps memory flat across a 1.3 GB file.
    """

    def __init__(self, rows: Iterator[str], encoding: str = "utf-8") -> None:
        super().__init__()
        self._rows = rows
        self._encoding = encoding
        self._buf = bytearray()
        self._exhausted = False

    def readable(self) -> bool:
        return True

    def _fill(self, size: int) -> None:
        while not self._exhausted and len(self._buf) < size:
            try:
                self._buf += next(self._rows).encode(self._encoding)
            except StopIteration:
                self._exhausted = True

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            # Unbounded reads would defeat the point of streaming; psycopg
            # always passes a positive block size.
            raise ValueError("CopyStream requires a bounded read size")
        self._fill(size)
        block = bytes(self._buf[:size])
        del self._buf[:size]
        return block

    def readline(self, size: int = -1) -> bytes:  # type: ignore[override]
        while not self._exhausted and b"\n" not in self._buf:
            self._fill(len(self._buf) + 4096)
        idx = self._buf.find(b"\n")
        if idx == -1:
            idx = len(self._buf) - 1
        line = bytes(self._buf[: idx + 1])
        del self._buf[: idx + 1]
        return line


def copy_into(cursor, copy_sql: str, rows: Iterator[str]) -> None:
    """Run COPY FROM STDIN against either psycopg2 or psycopg3."""
    stream = CopyStream(rows)
    raw = getattr(cursor, "cursor", cursor)
    if hasattr(raw, "copy_expert"):  # psycopg2
        raw.copy_expert(copy_sql, stream, size=CHUNK_BYTES)
        return
    with raw.copy(copy_sql) as copy:  # psycopg3
        while block := stream.read(CHUNK_BYTES):
            copy.write(block)
