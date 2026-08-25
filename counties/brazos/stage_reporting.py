"""Output adapter shared by Brazos refresh stages.

The stage modules can run from a Django management command or directly in a
test.  Their interface only needs Django's existing ``stdout`` and ``style``
objects, so the null adapter keeps the source-processing implementation free
of a command dependency.
"""

from __future__ import annotations

from typing import Protocol


class _Output(Protocol):
    def write(self, message: str) -> None: ...


class _Style(Protocol):
    def SUCCESS(self, message: str) -> str: ...

    def WARNING(self, message: str) -> str: ...


class StageReporter(Protocol):
    stdout: _Output
    style: _Style


class _SilentOutput:
    def write(self, message: str) -> None:
        pass


class _PlainStyle:
    @staticmethod
    def SUCCESS(message: str) -> str:
        return message

    @staticmethod
    def WARNING(message: str) -> str:
        return message


class SilentStageReporter:
    """Discard output while preserving the command reporter's small interface."""

    stdout = _SilentOutput()
    style = _PlainStyle()
