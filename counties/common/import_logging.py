"""Capture existing county diagnostics as durable operation evidence."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from threading import get_ident


@contextmanager
def import_warnings(logger_name: str) -> Iterator[list[str]]:
    warnings: list[str] = []
    thread_id = get_ident()

    class OperationWarnings(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.thread == thread_id:
                warnings.append(record.getMessage())

    handler = OperationWarnings(level=logging.WARNING)
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    try:
        yield warnings
    finally:
        logger.removeHandler(handler)
