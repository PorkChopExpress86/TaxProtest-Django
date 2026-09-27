"""Harris readiness: the web serves exactly what coverage qualification measures."""

from decimal import Decimal
from uuid import uuid4

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from counties.common.models import ImportCandidate, ImportOperation
from counties.common.tax_models import PropertyJurisdictionExemption, TaxUnitRate
from counties.harris.adapter import adapter
from counties.harris.etl_pipeline.candidate import outcome_populations
from counties.harris.models import BuildingDetail, PropertyRecord

OUTCOMES = {
    "search": "search_ready",
    "comparable": "comparable_ready",
    "report": "report_ready",
    "tax": "tax_impact_ready",
}


def publish_year(year):
    ImportCandidate.objects.create(
        county="harris",
        operation=ImportOperation.objects.create(
            county="harris", intent="full", status="published"
        ),
        state="published",
        storage_schema=f"harris_candidate_{uuid4().hex}",
        evidence={"property_source_year": year},
    )


def harris_property(
    key,
    *,
    residential=True,
    data_ready=True,
    located=True,
    equity=True,
    rooms=True,
    exemption_year=None,
):
    prop = PropertyRecord.objects.create(
        account_number=key,
        is_residential=residential,
        is_data_ready=data_ready,
        latitude=29.7 if located else None,
        longitude=-95.4 if located else None,
        assessed_value=250000 if equity else None,
    )
    BuildingDetail.objects.create(
        property=prop,
        account_number=key,
        building_number=1,
        heat_area=1800 if equity else None,
        bedrooms=3 if rooms else None,
        bathrooms=2 if rooms else None,
    )
    if exemption_year is not None:
        PropertyJurisdictionExemption.objects.create(
            county="harris",
            tax_year=exemption_year,
            account_number=key,
            tax_unit_code="A",
            taxable_value=250000,
        )
    return prop


def add_distant_report_pool(count=3):
    """Qualifying records far outside any similarity radius, so reports have a pool."""
    for number in range(count):
        prop = harris_property(f"POOL{number}")
        prop.latitude, prop.longitude = 31.5, -97.0
        prop.save(update_fields=["latitude", "longitude"])


class HarrisReadinessContractTests(TestCase):
    def setUp(self):
        publish_year(2026)
        TaxUnitRate.objects.create(
            county="harris", tax_year=2026, tax_unit_code="A", adopted_rate=Decimal("0.01")
        )
        for number in range(4):
            harris_property(f"TAX{number}", exemption_year=2026)
        harris_property("NO-EXEMPTION")
        harris_property("NO-COORDINATES", located=False)
        harris_property("NO-EQUITY", equity=False)
        harris_property("COMMERCIAL", residential=False)
        harris_property("INCOMPLETE", data_ready=False, located=False, rooms=False)

    def test_adapter_capabilities_equal_coverage_population_membership(self):
        populations = outcome_populations(adapter.published_year())
        for key in PropertyRecord.objects.values_list("account_number", flat=True):
            capabilities = adapter.capabilities(key)
            for outcome, attribute in OUTCOMES.items():
                with self.subTest(key=key, outcome=outcome):
                    population = populations[outcome]
                    ready = getattr(capabilities, attribute)
                    self.assertEqual(ready, key in population.eligible)
                    if not ready:
                        self.assertEqual(
                            capabilities.reasons[outcome], "; ".join(population.exclusions[key])
                        )

    def test_properties_outside_the_search_invariant_are_never_search_ready(self):
        for key in ("COMMERCIAL", "INCOMPLETE"):
            with self.subTest(key=key):
                self.assertFalse(adapter.capabilities(key).search_ready)

    def test_report_is_refused_with_the_coverage_reason(self):
        response = self.client.get(reverse("protest_analysis", args=["NO-EQUITY"]))
        self.assertContains(response, "Positive assessed value, living area and coordinates")
        self.assertIsNone(response.context.get("dossier"))


class HarrisCompleteReadinessRuleTests(TestCase):
    def setUp(self):
        publish_year(2026)
        TaxUnitRate.objects.create(
            county="harris", tax_year=2026, tax_unit_code="A", adopted_rate=Decimal("0.01")
        )
        for number in range(4):
            harris_property(f"TAX{number}", exemption_year=2026)

    def test_each_unready_outcome_names_its_missing_input(self):
        harris_property("NO-EQUITY", equity=False)
        harris_property("NO-EXEMPTION")
        harris_property("UNRATED-UNIT", exemption_year=2026)
        PropertyJurisdictionExemption.objects.create(
            county="harris",
            tax_year=2026,
            account_number="UNRATED-UNIT",
            tax_unit_code="B",
            taxable_value=250000,
        )
        expected = {
            "NO-EQUITY": ("report", "Positive assessed value, living area and coordinates"),
            "NO-EXEMPTION": ("tax", "Matching-year jurisdiction and exemption rows"),
            "UNRATED-UNIT": ("tax", "Adopted 2026 rate unavailable for taxing unit B"),
        }
        for key, (outcome, reason) in expected.items():
            with self.subTest(key=key):
                capabilities = adapter.capabilities(key)
                self.assertFalse(getattr(capabilities, OUTCOMES[outcome]))
                self.assertIn(reason, capabilities.reasons[outcome])
        self.assertTrue(adapter.capabilities("TAX0").tax_impact_ready)

    def test_report_support_counts_only_the_qualifying_pool(self):
        PropertyRecord.objects.filter(account_number__in=["TAX2", "TAX3"]).update(
            is_residential=False
        )
        populations = outcome_populations(2026)
        self.assertFalse(populations["report"].supported)
        self.assertFalse(populations["tax"].supported)
        capabilities = adapter.capabilities("TAX0")
        self.assertIn("At least three qualifying comparables", capabilities.reasons["report"])
        self.assertIn("At least three qualifying comparables", capabilities.reasons["tax"])

    def test_tax_becomes_ready_when_rates_arrive_after_publication(self):
        TaxUnitRate.objects.all().delete()
        self.assertFalse(adapter.capabilities("TAX0").tax_impact_ready)
        TaxUnitRate.objects.create(
            county="harris", tax_year=2026, tax_unit_code="A", adopted_rate=Decimal("0.01")
        )
        self.assertTrue(adapter.capabilities("TAX0").tax_impact_ready)
        self.assertIn("TAX0", outcome_populations(2026)["tax"].eligible)


class HarrisReadinessQueryBoundTests(TestCase):
    def queries_for_one_property(self, dataset_size):
        publish_year(2026)
        TaxUnitRate.objects.create(
            county="harris", tax_year=2026, tax_unit_code="A", adopted_rate=Decimal("0.01")
        )
        for number in range(dataset_size):
            harris_property(f"P{number:03d}", exemption_year=2026)
        with CaptureQueriesContext(connection) as queries:
            capabilities = adapter.capabilities("P000")
        self.assertTrue(capabilities.tax_impact_ready)
        self.assertFalse(any("row_to_json" in query["sql"] for query in queries))
        return len(queries)

    def test_unsupported_pool_readiness_does_not_scan_unqualified_records(self):
        def queries(unqualified):
            PropertyRecord.objects.all().delete()
            harris_property("SUBJECT")
            for number in range(unqualified):
                harris_property(f"NO-EQUITY{number:03d}", equity=False)
            with CaptureQueriesContext(connection) as captured:
                capabilities = adapter.capabilities("SUBJECT")
            self.assertIn("At least three qualifying comparables", capabilities.reasons["report"])
            return len(captured)

        self.assertEqual(queries(5), queries(60))

    def test_single_property_readiness_does_not_scale_with_the_dataset(self):
        small = self.queries_for_one_property(5)
        PropertyRecord.objects.all().delete()
        ImportCandidate.objects.all().delete()
        ImportOperation.objects.all().delete()
        TaxUnitRate.objects.all().delete()
        PropertyJurisdictionExemption.objects.all().delete()
        large = self.queries_for_one_property(60)
        self.assertEqual(small, large)
