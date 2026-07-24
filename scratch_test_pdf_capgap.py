"""Scratch: exercise protest_analysis_pdf with cap-gap fixtures for every scenario shape."""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from data.models import AssessmentHistory, BuildingDetail, PropertyRecord


class PdfCapGapScratchTests(TestCase):
    def setUp(self):
        self.target = PropertyRecord.objects.create(
            address="16213 Wall St",
            city="Houston",
            zipcode="77040",
            owner_name="T",
            account_number="PDFCAP",
            street_number="16213",
            street_name="Wall St",
            assessed_value=370000,
            building_area=2000,
            latitude=29.8,
            longitude=-95.5,
        )
        self.building = BuildingDetail.objects.create(
            property=self.target,
            account_number="PDFCAP",
            building_number=1,
            heat_area=2000,
            bedrooms=4,
            bathrooms=2,
            quality_code="B",
            year_built=2005,
            is_active=True,
        )
        self.history = AssessmentHistory.objects.create(
            account_number="PDFCAP",
            tax_year=2026,
            assessed_value=390000,
            appraised_value=355000,
            market_value=390000,
            cap_account="Y",
        )
        self.comp = PropertyRecord.objects.create(
            address="100 Similar Ln",
            city="Houston",
            zipcode="77040",
            owner_name="C",
            account_number="PDFCMP",
            street_number="100",
            street_name="Similar Ln",
            assessed_value=320000,
            building_area=2000,
            latitude=29.81,
            longitude=-95.5,
        )
        self.comp_building = BuildingDetail.objects.create(
            property=self.comp,
            account_number="PDFCMP",
            building_number=1,
            heat_area=2000,
            bedrooms=4,
            bathrooms=2,
            quality_code="B",
            year_built=2004,
            is_active=True,
        )

    def _result(self):
        return {
            "property": self.comp,
            "building": self.comp_building,
            "features": [],
            "distance": 0.5,
            "similarity_score": 75.0,
            "score_breakdown": [],
        }

    def _pdf_text(self):
        response = self.client.get(reverse("protest_analysis_pdf", args=["PDFCAP"]))
        assert response.status_code == 200, response.status_code
        assert response.content.startswith(b"%PDF")
        return response.content.decode("latin-1", errors="ignore")

    @patch("taxprotest.views.find_similar_properties")
    def test_effective_scenario_in_pdf(self, mock_find):
        mock_find.return_value = [self._result()]
        text = self._pdf_text()
        assert "Cap Gap Analysis" in text, text[-2000:]
        assert "Target is below the capped value" in text
        assert "Current-Year Taxable Reduction at Target: $35,000.00" in text

    @patch("taxprotest.views.find_similar_properties")
    def test_no_target_scenario_in_pdf(self, mock_find):
        mock_find.return_value = []
        text = self._pdf_text()
        assert "Cap Gap Analysis" in text
        assert "No comparable-based target available" in text
        assert "Reduction at Target" not in text

    @patch("taxprotest.views.find_similar_properties")
    def test_missing_scenario_omits_block(self, mock_find):
        AssessmentHistory.objects.all().delete()
        mock_find.return_value = []
        text = self._pdf_text()
        assert "Cap Gap Analysis" not in text

    @patch("taxprotest.views.find_similar_properties")
    def test_blocked_scenario_in_pdf(self, mock_find):
        self.comp.assessed_value = 370000
        self.comp.save()
        mock_find.return_value = [self._result()]
        text = self._pdf_text()
        assert "Cap gap blocks current-year savings" in text
        assert "Next-Year Taxable Reduction at Target: $20,500.00" in text
