"""Residential property classification helpers for HCAD real account data."""

from __future__ import annotations

# Based on var/extracted/Code_description_real/desc_r_01_state_class.txt and
# the HCAD docs under docs/hcad_docs/. HCAD uses broader residential-like
# state classes than this app wants. For the app's house-focused workflow,
# treat only house-style parcels as residential and exclude condo, auxiliary,
# and multifamily classes that do not reliably satisfy building/room readiness.
RESIDENTIAL_STATE_CLASSES = frozenset(
    {
        "A1",  # Single-family
        "A2",  # Mobile homes
        "A4",  # 1/2 duplex
        "E1",  # Farm & ranch improved
    }
)


def normalize_state_class(value: str | None) -> str:
    """Normalize a raw HCAD state class code for comparison."""
    return str(value or "").strip().upper()


def is_residential_state_class(value: str | None) -> bool:
    """Return True when the HCAD state class should be treated as residential."""
    return normalize_state_class(value) in RESIDENTIAL_STATE_CLASSES
