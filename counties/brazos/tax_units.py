"""Brazos taxing units that levy no ad valorem tax (ADR-0019).

Each entry needs source evidence that the unit levies nothing. A unit missing
from this list still needs an adopted rate, so a new zone surfaces as a missing
rate rather than being assumed to levy nothing.
"""

from collections.abc import Mapping

_REINVESTMENT_ZONE = (
    "Tax increment reinvestment zone: it levies no tax; participating units' rates apply"
)

# Units on BCAD certified entity rows that the BCAD adopted-rates page never lists.
NON_LEVYING_UNITS: Mapping[str, str] = {
    "CAD": "Appraisal district: funded by the taxing units, it levies no tax",
    "ZRFND": "BCAD refund entity: not a taxing unit, it levies no tax",
    "TZ19C": _REINVESTMENT_ZONE,  # CS TAX INCREMENT ZONE#19 CSMD-E
    "TZ21B": _REINVESTMENT_ZONE,  # BRYAN TAX INCREMENT ZONE #21
    "TZ22B": _REINVESTMENT_ZONE,  # BRYAN TAX INCREMENT ZONE #22
}
