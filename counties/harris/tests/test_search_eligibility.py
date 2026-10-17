"""Harris search lists only records the site can analyse (residential and data-ready).

ADR-0008 and the Harris readiness invariant: queryable Harris records satisfy both
``is_residential=True`` and ``is_data_ready=True``. Search is driven through the
shared routes, so the paginated page and the search CSV export are checked alike.
"""

import csv
from io import StringIO

from django.test import TestCase
from django.urls import reverse

from counties.harris.models import PropertyRecord

ZIP_CODE = "77070"
ELIGIBLE = "ELIGIBLE01"
NOT_DATA_READY = "NOTREADY01"
NOT_RESIDENTIAL = "NOTRESID01"
NEITHER = "NEITHER001"


def make_record(account_number, *, is_residential, is_data_ready):
    return PropertyRecord.objects.create(
        account_number=account_number,
        is_residential=is_residential,
        is_data_ready=is_data_ready,
        address=f"{account_number} Search Ln",
        city="Houston",
        zipcode=ZIP_CODE,
        owner_name=f"Owner {account_number}",
        street_number=account_number[-2:],
        street_name="Search Ln",
        value=200000,
    )


class HarrisSearchListsOnlyEligibleRecordsTests(TestCase):
    def setUp(self):
        make_record(ELIGIBLE, is_residential=True, is_data_ready=True)
        make_record(NOT_DATA_READY, is_residential=True, is_data_ready=False)
        make_record(NOT_RESIDENTIAL, is_residential=False, is_data_ready=True)
        make_record(NEITHER, is_residential=False, is_data_ready=False)

    def test_search_page_lists_only_the_residential_data_ready_record(self):
        response = self.client.get(reverse("index"), {"zip_code": ZIP_CODE})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["account_number"] for row in response.context["results"]], [ELIGIBLE])
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        for account_number in (NOT_DATA_READY, NOT_RESIDENTIAL, NEITHER):
            with self.subTest(account_number=account_number):
                self.assertNotContains(response, account_number)

    def test_eligible_record_keeps_its_similar_properties_link(self):
        response = self.client.get(reverse("index"), {"zip_code": ZIP_CODE})

        self.assertContains(response, reverse("similar_properties", args=[ELIGIBLE]))

    def test_search_export_lists_only_the_residential_data_ready_record(self):
        response = self.client.get(reverse("export_csv"), {"zip_code": ZIP_CODE})

        self.assertEqual(response.status_code, 200)
        rows = list(csv.DictReader(StringIO(response.content.decode())))
        self.assertEqual([row["Account Number"] for row in rows], [ELIGIBLE])

    def test_text_filters_cannot_surface_an_ineligible_record(self):
        for route in ("index", "export_csv"):
            with self.subTest(route=route):
                response = self.client.get(reverse(route), {"street_name": "Search Ln"})

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, ELIGIBLE)
                for account_number in (NOT_DATA_READY, NOT_RESIDENTIAL, NEITHER):
                    self.assertNotContains(response, account_number)
