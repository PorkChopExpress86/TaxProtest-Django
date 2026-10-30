"""Tests for the BCAD portal scraper.

Patches ``requests.get`` to return a fixture HTML page with .zip links of
varying years, and asserts the loader picks the latest year, or the requested one.
"""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock, patch

import requests
from django.core.management import CommandError
from django.test import SimpleTestCase

from counties.brazos.cad_refresh import BCAD_PORTAL_URL, CadRefreshStage
from counties.brazos.property_import import RefreshOptions


class ScrapeArchiveTests(SimpleTestCase):
    def _mock_response(self, html: str) -> MagicMock:
        resp = MagicMock()
        resp.text = html
        resp.raise_for_status = MagicMock()
        return resp

    def test_picks_latest_year(self):
        html = """
        <html><body>
          <a href="/files/certified_2022.zip">2022 Certified</a>
          <a href="/files/certified_2023.zip">2023 Certified</a>
          <a href="/files/certified_2024.zip">2024 Certified</a>
        </body></html>
        """
        with patch(
            "counties.brazos.cad_refresh.requests.get",
            return_value=self._mock_response(html),
        ) as mock_get:
            url, year = CadRefreshStage()._scrape_archive(BCAD_PORTAL_URL)

        self.assertTrue(url.endswith("certified_2024.zip"))
        self.assertEqual(year, 2024)
        mock_get.assert_called_once()

    def test_falls_back_to_last_link_when_no_year(self):
        html = """
        <html><body>
          <a href="/files/certified.zip">Certified (no year)</a>
          <a href="/files/certified_latest.zip">Latest</a>
        </body></html>
        """
        with patch(
            "counties.brazos.cad_refresh.requests.get",
            return_value=self._mock_response(html),
        ):
            url, year = CadRefreshStage()._scrape_archive(BCAD_PORTAL_URL)

        self.assertTrue(url.endswith("certified_latest.zip"))
        # Year is unknown — falls back to 0, indicating caller should pass --year.
        self.assertEqual(year, 0)

    def test_raises_when_no_zip_links_found(self):
        html = "<html><body><p>No downloads yet.</p></body></html>"
        with patch(
            "counties.brazos.cad_refresh.requests.get",
            return_value=self._mock_response(html),
        ):
            with self.assertRaises(CommandError):
                CadRefreshStage()._scrape_archive(BCAD_PORTAL_URL)

    def test_raises_when_request_fails(self):
        with patch(
            "counties.brazos.cad_refresh.requests.get",
            side_effect=requests.ConnectionError("connection refused"),
        ):
            with self.assertRaises(CommandError):
                CadRefreshStage()._scrape_archive(BCAD_PORTAL_URL)


class RequestedYearSelectionTests(SimpleTestCase):
    """``--year`` picks the archive before anything is downloaded."""

    HTML = """
    <html><body>
      <a href="/files/certified_2023.zip">2023 Certified</a>
      <a href="/files/certified_2024.zip">2024 Certified</a>
      <a href="/files/certified_2025.zip">2025 Certified</a>
    </body></html>
    """

    def _prepare(self, tax_year: int, tmp: str):
        response = MagicMock(text=self.HTML)
        stage = CadRefreshStage()
        with (
            self.settings(
                BCAD_DOWNLOAD_DIR=f"{tmp}/downloads", BCAD_EXTRACT_DIR=f"{tmp}/extracted"
            ),
            patch("counties.brazos.cad_refresh.working_source_root", side_effect=lambda p: p),
            patch("counties.brazos.cad_refresh.requests.get", return_value=response),
            patch("counties.brazos.cad_refresh.download_archive") as download,
        ):
            try:
                return stage.prepare(RefreshOptions(tax_year=tax_year, dry_run=True)), download
            except CommandError as exc:
                return exc, download

    def test_requested_year_selects_its_own_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            preparation, download = self._prepare(2024, tmp)

        self.assertEqual(preparation.source_year, 2024)
        self.assertEqual(preparation.target_year, 2024)
        download.assert_called_once()
        self.assertTrue(download.call_args.args[0].endswith("certified_2024.zip"))

    def test_missing_requested_year_fails_before_any_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            error, download = self._prepare(2019, tmp)

        self.assertIsInstance(error, CommandError)
        self.assertIn("2019", str(error))
        download.assert_not_called()
