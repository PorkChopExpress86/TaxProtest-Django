"""View tests covering both counties.

Harris behaviour is pinned by taxprotest/tests/test_views.py; these cover Brazos
and the county resolution that routes between them.
"""

from __future__ import annotations

import csv
from decimal import Decimal
from io import StringIO

from django.test import TestCase
from django.urls import reverse

from data.models import PropertyAccount, PropertyImprovementDetail, PropertyRecord


def make_account(prop_id: int, *, lat="30.6280", lon="-96.3344", year=2025, **kwargs):
    defaults = {
        "prop_type_cd": "R",
        "owner_name": f"OWNER {prop_id}",
        "situs_num": str(4000 + prop_id % 1000),
        "situs_street": "ROCK BEND",
        "situs_street_suffix": "DR",
        "land_acres": Decimal("0.2000"),
        "appraised_val": Decimal("360000"),
        "assessed_val": Decimal("360000"),
        "appraised_val_prod_loss": Decimal("360000"),
        "assessed_val_prod_loss": Decimal("360000"),
        "latitude": Decimal(lat),
        "longitude": Decimal(lon),
    }
    defaults.update(kwargs)
    return PropertyAccount.objects.create(prop_id=prop_id, tax_year=year, **defaults)


def add_dwelling(prop_id: int, area="1800", year_built=2010, klass="RV4", tax_year=2025):
    PropertyImprovementDetail.objects.create(
        prop_id=prop_id,
        tax_year=tax_year,
        imp_id=1,
        imprv_det_id=prop_id * 10,
        imprv_det_type_cd="MA",
        imprv_det_type_desc="MAIN AREA",
        imprv_det_class_cd=klass,
        imprv_det_area=Decimal(area),
        yr_built=year_built,
    )


class BrazosSearchTests(TestCase):
    def setUp(self):
        make_account(349630)
        add_dwelling(349630)
        PropertyRecord.objects.create(
            address="1 HARRIS ST",
            account_number="HARRIS1",
            street_number="1",
            street_name="HARRIS ST",
            zipcode="77001",
            owner_name="HARRIS OWNER",
            value=200000,
        )

    def test_search_returns_brazos_rows(self):
        response = self.client.get(reverse("index"), {"street_name": "ROCK BEND"})
        self.assertEqual(response.status_code, 200)
        results = response.context["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["county"], "brazos")
        self.assertEqual(results[0]["county_label"], "Brazos")
        self.assertEqual(results[0]["account_number"], "349630")

    def test_search_spans_both_counties_by_default(self):
        response = self.client.get(reverse("index"), {"last_name": "OWNER"})
        counties = {r["county"] for r in response.context["results"]}
        self.assertEqual(counties, {"hcad", "brazos"})

    def test_county_filter_restricts_results(self):
        response = self.client.get(reverse("index"), {"last_name": "OWNER", "county": "hcad"})
        self.assertEqual({r["county"] for r in response.context["results"]}, {"hcad"})

    def test_unknown_county_falls_back_to_all(self):
        response = self.client.get(reverse("index"), {"last_name": "OWNER", "county": "travis"})
        self.assertEqual(response.context["county"], "")
        self.assertEqual({r["county"] for r in response.context["results"]}, {"hcad", "brazos"})

    def test_county_choices_available_to_template(self):
        response = self.client.get(reverse("index"))
        self.assertIn(("brazos", "Brazos"), response.context["county_choices"])

    def test_unpublished_factors_render_as_missing(self):
        response = self.client.get(reverse("index"), {"street_name": "ROCK BEND"})
        row = response.context["results"][0]
        self.assertIsNone(row["bedrooms"])
        self.assertIsNone(row["bathrooms"])
        # Living area is derived from the improvement detail rows.
        self.assertEqual(row["building_area"], 1800.0)


class BrazosExportTests(TestCase):
    def setUp(self):
        make_account(349630)
        add_dwelling(349630)

    def test_csv_includes_county_column_last(self):
        response = self.client.get(reverse("export_csv"), {"street_name": "ROCK BEND"})
        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(StringIO(response.content.decode())))
        # Appended, so existing column positions are unchanged.
        self.assertEqual(rows[0][-1], "County")
        self.assertEqual(rows[0][0], "Account Number")
        self.assertEqual(rows[1][-1], "Brazos")
        self.assertEqual(rows[1][0], "349630")


class BrazosSimilarPropertiesTests(TestCase):
    def setUp(self):
        make_account(349630)
        add_dwelling(349630)
        for offset in range(1, 4):
            pid = 349630 + offset
            make_account(pid, lat=f"30.628{offset}", lon="-96.3344")
            add_dwelling(pid, area=str(1800 + offset * 20))

    def test_renders_brazos_comparables(self):
        response = self.client.get(reverse("similar_properties", args=["349630"]))
        self.assertEqual(response.status_code, 200)
        results = response.context["results"]
        self.assertTrue(results[0]["is_target"])
        self.assertEqual(response.context["county"], "brazos")
        self.assertEqual(response.context["county_label"], "Brazos")
        self.assertGreater(len(results), 1)
        self.assertTrue(all(r["county"] == "brazos" for r in results))

    def test_explicit_county_is_honoured(self):
        response = self.client.get(
            reverse("similar_properties", args=["349630"]), {"county": "brazos"}
        )
        self.assertEqual(response.context["county"], "brazos")

    def test_wrong_county_reports_not_found(self):
        response = self.client.get(
            reverse("similar_properties", args=["349630"]), {"county": "hcad"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("error", response.context)

    def test_account_without_coordinates_explains_why(self):
        make_account(555001, latitude=None, longitude=None)
        response = self.client.get(reverse("similar_properties", args=["555001"]))
        self.assertIn("location data", response.context["error"])

    def test_target_card_uses_neutral_fields(self):
        response = self.client.get(reverse("similar_properties", args=["349630"]))
        content = response.content.decode()
        self.assertIn("Brazos County", content)
        self.assertIn("ROCK BEND DR", content)
        # The subject property must not silently render as a Harris record.
        self.assertNotIn("Harris County", content)


class BrazosProtestAnalysisTests(TestCase):
    def setUp(self):
        make_account(349630, year=2025)
        add_dwelling(349630)
        # A prior year, which is where Brazos assessment history comes from.
        make_account(
            349630,
            year=2024,
            assessed_val_prod_loss=Decimal("330000"),
            appraised_val_prod_loss=Decimal("330000"),
            appraised_val=Decimal("330000"),
        )
        for offset in range(1, 5):
            pid = 349630 + offset
            make_account(pid, lat=f"30.628{offset}")
            add_dwelling(pid, area=str(1800 + offset * 10))

    def test_page_renders_for_brazos(self):
        response = self.client.get(reverse("protest_analysis", args=["349630"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["county_label"], "Brazos")

    def test_history_is_built_from_per_year_account_rows(self):
        response = self.client.get(reverse("protest_analysis", args=["349630"]))
        history = response.context["assessment_history"]
        self.assertEqual([row["tax_year"] for row in history], [2025, 2024])
        # 330,000 -> 360,000 is a 9.09% rise.
        self.assertEqual(history[0]["increase_percent"], Decimal("9.09"))

    def test_cap_status_matches_the_harris_shape(self):
        response = self.client.get(reverse("protest_analysis", args=["349630"]))
        cap = response.context["assessment_history"][0]["cap_status"]
        # The template reads .status and .label, so a plain string would break it.
        self.assertIn(cap["status"], {"within_limit", "over_limit", "unknown"})
        self.assertTrue(cap["label"])

    def test_capped_property_is_flagged(self):
        PropertyAccount.objects.filter(prop_id=349630, tax_year=2025).update(
            ten_percent_cap=Decimal("15000"), hs_exempt=True
        )
        response = self.client.get(reverse("protest_analysis", args=["349630"]))
        cap = response.context["assessment_history"][0]["cap_status"]
        self.assertEqual(cap["status"], "over_limit")
        self.assertEqual(cap["cap_type"], "homestead")

    def test_tax_impact_degrades_without_rate_data(self):
        # Brazos has no TaxUnitRate/PropertyJurisdictionExemption rows loaded.
        response = self.client.get(reverse("protest_analysis", args=["349630"]))
        self.assertEqual(response.context["tax_impact"].completeness, "missing")
        self.assertIn("have not been loaded for Brazos", response.content.decode())

    def test_report_names_the_correct_county(self):
        content = self.client.get(reverse("protest_analysis", args=["349630"])).content.decode()
        self.assertIn("Brazos County Property Tax Protest Analysis", content)
        self.assertNotIn("Harris County Property Tax Protest Analysis", content)

    def test_pdf_names_the_correct_county(self):
        response = self.client.get(reverse("protest_analysis_pdf", args=["349630"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(b"Brazos County Property Tax Protest Evidence Report", response.content)

    def test_csv_export_works_for_brazos(self):
        response = self.client.get(reverse("protest_analysis_export", args=["349630"]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])

    def test_unknown_account_still_404s(self):
        response = self.client.get(reverse("protest_analysis", args=["nonexistent"]))
        self.assertEqual(response.status_code, 404)
