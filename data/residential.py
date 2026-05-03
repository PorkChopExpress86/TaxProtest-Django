"""Residential property classification helpers for HCAD real account data."""

from __future__ import annotations

from typing import Optional

# Based on var/extracted/Code_description_real/desc_r_01_state_class.txt
# Residential-like classes are limited to property types that should resolve
# to a residential parcel in this app. Readiness is still enforced separately.
RESIDENTIAL_STATE_CLASSES = frozenset(
    {
        "A1",  # Single-family
        "A2",  # Mobile homes
        "A3",  # Auxiliary buildings
        "A4",  # 1/2 duplex
        "B1",  # Multi-family
        "B2",  # Two-family
        "B3",  # Three-family
        "B4",  # Four-or-more-family
        "E1",  # Farm & ranch improved
        "Z1",  # Condo - apartment conversion
        "Z2",  # Condo - fee simple townhouse
        "Z3",  # Condo - townhouse (2+ stories)
        "Z4",  # Condo - apartment style
        "Z5",  # Condo - high rise
    }
)


def normalize_state_class(value: Optional[str]) -> str:
    """Normalize a raw HCAD state class code for comparison."""
    return str(value or "").strip().upper()



def is_residential_state_class(value: Optional[str]) -> bool:
    """Return True when the HCAD state class should be treated as residential."""
    return normalize_state_class(value) in RESIDENTIAL_STATE_CLASSES
