"""Assessment-history rows for the shared report, from the shared table.

Historical functions now live cohesively in ``counties.common.analysis``.
This module re-exports them for backwards compatibility.
"""

from __future__ import annotations

from counties.common.analysis import assessment_history_rows, history_availability_notice

__all__ = ["assessment_history_rows", "history_availability_notice"]
