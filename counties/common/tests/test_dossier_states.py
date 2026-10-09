"""Each dossier state maps to one response on each shared route, for both real counties.

Ready serves the page or download. Unavailable (found, but not ready) serves the page
with the real reason and refuses the CSV and PDF with a 400. Not found (no such key or,
for Harris, a record that is not search-ready) is a 404 on all four routes. The reasons
and bodies are pinned in ``test_shared_route_characterization``; this is the mapping table.
"""

from __future__ import annotations

from django.test import TestCase

from counties.common.analysis import DossierStatus
from counties.common.tests.test_shared_route_characterization import (
    BRAZOS,
    COMPARABLES,
    COUNTIES,
    CSV,
    HARRIS,
    PDF,
    REPORT,
    add_brazos_account,
    get,
)
from counties.harris.tests.test_readiness import harris_property

RESPONSE_STATUS = {
    DossierStatus.READY: {COMPARABLES: 200, REPORT: 200, CSV: 200, PDF: 200},
    DossierStatus.UNAVAILABLE: {COMPARABLES: 200, REPORT: 200, CSV: 400, PDF: 400},
    DossierStatus.NOT_FOUND: {COMPARABLES: 404, REPORT: 404, CSV: 404, PDF: 404},
}

#: Per county, one key in each state. Unavailable keys have no coordinates.
KEYS = {
    HARRIS.slug: {
        DossierStatus.READY: HARRIS.key,
        DossierStatus.UNAVAILABLE: "NOLOC002",
        DossierStatus.NOT_FOUND: HARRIS.unknown_key,
    },
    BRAZOS.slug: {
        DossierStatus.READY: BRAZOS.key,
        DossierStatus.UNAVAILABLE: "000000010098",
        DossierStatus.NOT_FOUND: BRAZOS.unknown_key,
    },
}


class DossierStateResponseTests(TestCase):
    def setUp(self):
        for county in COUNTIES:
            county.make_subject()
        harris_property(KEYS[HARRIS.slug][DossierStatus.UNAVAILABLE], located=False)
        add_brazos_account(KEYS[BRAZOS.slug][DossierStatus.UNAVAILABLE], located=False)

    def test_each_state_maps_to_its_response_on_every_route(self):
        for county in COUNTIES:
            for state, statuses in RESPONSE_STATUS.items():
                for route, expected in statuses.items():
                    with self.subTest(county=county.slug, state=state.value, route=route):
                        response = get(self.client, county, route, KEYS[county.slug][state])

                        self.assertEqual(response.status_code, expected)
