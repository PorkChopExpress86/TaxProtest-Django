"""Tests for the shared BCAD portal download helper."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import requests
from django.core.management import CommandError
from django.test import SimpleTestCase

from counties.brazos.portal import download_archive

FULL_ARCHIVE = b"complete archive bytes"


class FakeStream:
    def __init__(self, chunks: list[bytes], *, broken: bool = False):
        self.chunks = chunks
        self.broken = broken

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size: int):
        yield from self.chunks
        if self.broken:
            raise requests.exceptions.ChunkedEncodingError("connection broken")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class DownloadArchiveTests(SimpleTestCase):
    def assert_failed_download_leaves_nothing_and_next_run_completes(self, failing: FakeStream):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "archives" / "certified_2024.zip"
            complete = FakeStream([FULL_ARCHIVE[:9], FULL_ARCHIVE[9:]])

            with patch("counties.brazos.portal.requests.get", side_effect=[failing, complete]):
                with self.assertRaises(CommandError):
                    download_archive(
                        "https://example.test/a.zip",
                        destination,
                        force=False,
                        dry_run=False,
                        log=lambda _: None,
                    )
                self.assertEqual(sorted(destination.parent.iterdir()), [])

                result = download_archive(
                    "https://example.test/a.zip",
                    destination,
                    force=False,
                    dry_run=False,
                    log=lambda _: None,
                )

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), FULL_ARCHIVE)
            self.assertEqual(sorted(destination.parent.iterdir()), [destination])

    def test_interrupted_stream_leaves_nothing_and_next_run_downloads_again(self):
        self.assert_failed_download_leaves_nothing_and_next_run_completes(
            FakeStream([FULL_ARCHIVE[:9]], broken=True)
        )
