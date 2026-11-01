"""Shared responses and table exchanges exclude each other (ADR-0025).

A snapshot does not isolate an exchanged table, so each shared response holds the
publication lock shared for its whole response, and an exchange takes it exclusively
together with the table locks it needs.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from django.db import DatabaseError, OperationalError, connection, transaction

# How long an exchange waits, per attempt, for shared responses and other readers.
EXCHANGE_LOCK_TIMEOUT = "5s"
EXCHANGE_LOCK_ATTEMPTS = 3
# A two-key advisory lock, apart from the one-key county writer locks; its family key
# spells "TPRD", for TaxProtest published read.
_PUBLISHED_READ_LOCK = (0x54505244, 0)
_LOCK_NOT_AVAILABLE = "55P03"


@contextmanager
def published_read() -> Iterator[None]:
    """Keep publication from exchanging tables while one shared response reads them.

    Taken before the response's transaction begins, so its snapshot is never older
    than an exchange that committed while it waited.
    """
    if connection.vendor != "postgresql":
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock_shared(%s, %s)", _PUBLISHED_READ_LOCK)
    try:
        yield
    finally:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock_shared(%s, %s)", _PUBLISHED_READ_LOCK)
        except DatabaseError:
            pass  # a lost connection released the lock with its session


def lock_for_exchange(cursor, tables: Sequence[str]) -> None:
    """Hold the publication lock and ``tables`` exclusively until the transaction ends.

    Waits a bounded time for shared responses and other readers to finish, and raises
    without changing anything when they outlast every attempt.
    """
    cursor.execute("SHOW lock_timeout")
    previous = cursor.fetchone()[0]
    cursor.execute("SELECT set_config('lock_timeout', %s, true)", [EXCHANGE_LOCK_TIMEOUT])
    for attempt in range(EXCHANGE_LOCK_ATTEMPTS):
        try:
            with transaction.atomic():
                cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", _PUBLISHED_READ_LOCK)
                cursor.execute(f"LOCK TABLE {', '.join(tables)} IN ACCESS EXCLUSIVE MODE")
            break
        except OperationalError as exc:
            if getattr(exc.__cause__, "pgcode", None) != _LOCK_NOT_AVAILABLE:
                raise
            if attempt + 1 == EXCHANGE_LOCK_ATTEMPTS:
                raise OperationalError(
                    "Publication could not lock the published tables within "
                    f"{EXCHANGE_LOCK_ATTEMPTS} waits of {EXCHANGE_LOCK_TIMEOUT}; "
                    "retry once shared reads finish"
                ) from exc
    # The rest of the publication transaction keeps its usual lock waits.
    cursor.execute("SELECT set_config('lock_timeout', %s, true)", [previous])
