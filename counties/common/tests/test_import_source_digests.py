"""Source digests are read once per Import operation and never describe provenance."""

import hashlib
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from counties.common import import_source_digests as source_digests
from counties.common.import_audit import record_sources
from counties.common.import_source_digests import (
    PROVENANCE_FILENAME,
    carry_digests,
    digest_once,
    file_digest,
    tree_digests,
)
from counties.common.models import ImportOperation


class SourceDigestTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.root / "base" / "source.txt"
        self.source.parent.mkdir()
        self.source.write_text("first")
        self.hashed: Counter[Path] = Counter()
        real_hash = source_digests._hash

        def counted(path: Path) -> str:
            self.hashed[path] += 1
            return real_hash(path)

        self.enterContext(patch.object(source_digests, "_hash", side_effect=counted))

    def test_a_digest_is_read_once_per_operation_while_its_file_is_unchanged(self):
        with digest_once():
            file_digest(self.source)
            self.assertEqual(file_digest(self.source), hashlib.sha256(b"first").hexdigest())
        self.assertEqual(self.hashed[self.source], 1)

    def test_a_changed_file_is_read_again(self):
        with digest_once():
            file_digest(self.source)
            replacement = self.source.with_suffix(".part")
            replacement.write_text("other")
            replacement.replace(self.source)
            self.assertEqual(file_digest(self.source), hashlib.sha256(b"other").hexdigest())
        self.assertEqual(self.hashed[self.source], 2)

    def test_outside_an_operation_every_digest_is_read(self):
        file_digest(self.source)
        file_digest(self.source)
        self.assertEqual(self.hashed[self.source], 2)

    def test_a_completed_copy_takes_the_digests_read_from_its_source(self):
        copy = self.root / "copy"
        with digest_once():
            expected = tree_digests(self.source.parent)
            shutil.copytree(self.source.parent, copy)
            carry_digests(self.source.parent, copy)
            self.assertEqual(tree_digests(copy), expected)
        self.assertNotIn(copy / "source.txt", self.hashed)

    def test_a_source_changed_before_its_copy_gives_the_copy_no_digest(self):
        copy = self.root / "copy"
        with digest_once():
            tree_digests(self.source.parent)
            self.source.write_text("changed")
            shutil.copytree(self.source.parent, copy)
            carry_digests(self.source.parent, copy)
            self.assertEqual(
                tree_digests(copy), {"source.txt": hashlib.sha256(b"changed").hexdigest()}
            )
        self.assertEqual(self.hashed[copy / "source.txt"], 1)

    def test_provenance_files_are_not_sources(self):
        (self.source.parent / PROVENANCE_FILENAME).write_text("{}")
        (self.source.parent / f"{PROVENANCE_FILENAME}.partial").write_text("{")

        self.assertEqual(sorted(tree_digests(self.source.parent)), ["source.txt"])

    def test_a_directory_listing_records_no_provenance_file(self):
        (self.source.parent / PROVENANCE_FILENAME).write_text("{}")
        operation = ImportOperation(evidence={})

        record_sources(operation, sorted(self.source.parent.glob("*")))

        self.assertEqual(
            [source["path"] for source in operation.evidence["sources"]], [str(self.source)]
        )
