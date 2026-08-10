"""Tests for the BCAD portal scraper.

Patches ``requests.get`` to return a fixture HTML page with .zip links of
varying years, and asserts the loader picks the latest year.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests
from django.core.management import CommandError
from django.test import SimpleTestCase

from counties.brazos.management.commands.load_brazos_cad import BCAD_PORTAL_URL, Command


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
            "counties.brazos.management.commands.load_brazos_cad.requests.get",
            return_value=self._mock_response(html),
        ) as mock_get:
            url, year = Command()._scrape_archive(BCAD_PORTAL_URL)

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
            "counties.brazos.management.commands.load_brazos_cad.requests.get",
            return_value=self._mock_response(html),
        ):
            url, year = Command()._scrape_archive(BCAD_PORTAL_URL)

        self.assertTrue(url.endswith("certified_latest.zip"))
        # Year is unknown — falls back to 0, indicating caller should pass --year.
        self.assertEqual(year, 0)

    def test_raises_when_no_zip_links_found(self):
        html = "<html><body><p>No downloads yet.</p></body></html>"
        with patch(
            "counties.brazos.management.commands.load_brazos_cad.requests.get",
            return_value=self._mock_response(html),
        ):
            with self.assertRaises(CommandError):
                Command()._scrape_archive(BCAD_PORTAL_URL)

    def test_raises_when_request_fails(self):
        with patch(
            "counties.brazos.management.commands.load_brazos_cad.requests.get",
            side_effect=requests.ConnectionError("connection refused"),
        ):
            with self.assertRaises(CommandError):
                Command()._scrape_archive(BCAD_PORTAL_URL)
