"""A fake third county for proving the County registration is sufficient (ADR-0022).

It borrows a real candidate port, since the port is unchanged by registration, and
declares its own slug, writer lock key, warning logger and source roots.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from counties.common.county_registry import (
    CountyRegistration,
    isolated_registrations,
    register,
    registration_for,
)

FAKE_COUNTY = "travis"
FAKE_LOCK_KEY = 742199
FAKE_LOGGER = "travis_cad"


def fake_county_registration(**overrides) -> CountyRegistration:
    fields = {
        "slug": FAKE_COUNTY,
        "writer_lock_key": FAKE_LOCK_KEY,
        "warning_logger": FAKE_LOGGER,
        "source_roots": lambda: (Path("/downloads"), Path("/extracts")),
        **overrides,
    }
    return replace(registration_for("harris"), **fields)


@contextmanager
def registered_fake_county(**overrides) -> Iterator[CountyRegistration]:
    """Register the fake county for the block; the real registrations are restored after."""
    registration = fake_county_registration(**overrides)
    with isolated_registrations():
        register(registration)
        yield registration
