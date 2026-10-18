"""Refuse a delete-and-reload that would empty or sharply shrink a year's live rows.

The jurisdiction/exemption and assessment-history imports replace a tax year in
shared tables outside the Candidate lifecycle, so nothing else reviews what they
load before it is live.
"""

from __future__ import annotations

#: A reload must bring at least this share of the rows it replaces. Year-over-year
#: HCAD counts move by a few percent and an account-scope change by tens of percent;
#: a broken source loads nothing or a fraction of that.
MIN_REPLACEMENT_RATIO = 0.5


class ReplacementShrinkError(RuntimeError):
    """A reload staged no rows, or far fewer than the rows it would replace."""


def check_replacement(label: str, *, replacing: int, staged: int) -> None:
    if staged == 0 or staged < replacing * MIN_REPLACEMENT_RATIO:
        raise ReplacementShrinkError(
            f"{label}: refusing to replace {replacing:,} live rows with {staged:,} staged rows "
            f"(minimum {MIN_REPLACEMENT_RATIO:.0%} of the rows replaced, and never zero). "
            "Check the source file's layout before reloading."
        )
