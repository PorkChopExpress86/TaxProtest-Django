"""Tests for the Brazos CAD loader's record reader and COPY plumbing.

These cover the parts that operate on raw bytes before PostgreSQL sees anything,
which is where the format's sharp edges live.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from data.brazos_copy import CopyStream, encode_copy_row
from data.brazos_layouts import APPRAISAL_ENTITY, APPRAISAL_INFO
from data.management.commands import load_brazos_cad
from data.management.commands.load_brazos_cad import Command


def make_command(encoding: str = "latin-1") -> Command:
    command = Command()
    command.encoding = encoding
    command.verbosity = 0
    command.record_width = None
    return command


class CopyRowEncodingTests(SimpleTestCase):
    def test_none_becomes_null_token(self):
        self.assertEqual(encode_copy_row(["a", None, "b"]), "a\t\\N\tb\n")

    def test_empty_string_is_not_null(self):
        # An empty text field must arrive as '' so NOT NULL columns accept it.
        self.assertEqual(encode_copy_row(["", None]), "\t\\N\n")

    def test_escapes_delimiters_and_newlines(self):
        # A tab or newline inside a text field would otherwise corrupt the
        # record framing that COPY relies on.
        row = encode_copy_row(["has\ttab", "has\nnewline", "has\\backslash", "has\rcr"])
        self.assertEqual(row, "has\\ttab\thas\\nnewline\thas\\\\backslash\thas\\rcr\n")

    def test_escaped_row_has_no_raw_control_characters(self):
        row = encode_copy_row(["x\ty\nz\r\\"])
        self.assertEqual(row.count("\n"), 1)
        self.assertTrue(row.endswith("\n"))
        self.assertNotIn("\r", row)
        self.assertNotIn("\t", row[:-1].replace("\\t", ""))


class CopyStreamTests(SimpleTestCase):
    def test_streams_rows_across_arbitrary_block_sizes(self):
        rows = [f"row{i}\n" for i in range(50)]
        stream = CopyStream(iter(rows))
        chunks = []
        while block := stream.read(7):
            chunks.append(block)
        self.assertEqual(b"".join(chunks).decode(), "".join(rows))

    def test_read_returns_empty_at_end(self):
        stream = CopyStream(iter(["only\n"]))
        self.assertEqual(stream.read(1024), b"only\n")
        self.assertEqual(stream.read(1024), b"")

    def test_is_lazy_and_does_not_drain_the_iterator_upfront(self):
        consumed = []

        def rows():
            for i in range(1000):
                consumed.append(i)
                yield f"{i}\n"

        stream = CopyStream(rows())
        stream.read(4)
        # A generator-backed stream must not materialise the whole source; that
        # is the entire reason a 1.3 GB file fits in memory.
        self.assertLess(len(consumed), 10)

    def test_rejects_unbounded_read(self):
        with self.assertRaises(ValueError):
            CopyStream(iter(["a\n"])).read(-1)


class RecordReaderTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, text: str) -> Path:
        path = self.root / "sample.TXT"
        path.write_text(text, encoding="latin-1", newline="")
        return path

    def test_splits_crlf_terminated_records(self):
        path = self.write("AAAAA\r\nBBBBB\r\nCCCCC\r\n")
        self.assertEqual(list(make_command()._records(path, 5)), ["AAAAA", "BBBBB", "CCCCC"])

    def test_splits_lf_terminated_records(self):
        path = self.write("AAAAA\nBBBBB\n")
        self.assertEqual(list(make_command()._records(path, 5)), ["AAAAA", "BBBBB"])

    def test_handles_missing_final_terminator(self):
        path = self.write("AAAAA\r\nBBBBB")
        self.assertEqual(list(make_command()._records(path, 5)), ["AAAAA", "BBBBB"])

    def test_newline_embedded_in_a_field_does_not_split_the_record(self):
        """The real 2024 defect: a CR/LF inside an owner name.

        Read line by line, this yields a truncated record plus orphan fragments,
        and the truncated one silently loads with blank value fields. Read at the
        record width, the embedded newline is just field content.
        """
        path = self.write("AA\nAA\r\nBBBBB\r\n")
        records = list(make_command()._records(path, 5))
        self.assertEqual(records, ["AA\nAA", "BBBBB"])

    def test_record_spanning_multiple_embedded_newlines(self):
        path = self.write("A\nB\nC\r\nDDDDD\r\n")
        self.assertEqual(list(make_command()._records(path, 5)), ["A\nB\nC", "DDDDD"])

    def test_records_are_recovered_across_read_block_boundaries(self):
        # Force many refills so the CRLF-straddling logic is exercised.
        expected = [f"{i:05d}" for i in range(500)]
        path = self.write("".join(f"{r}\r\n" for r in expected))
        with mock.patch.object(load_brazos_cad, "READ_BLOCK_CHARS", 7):
            self.assertEqual(list(make_command()._records(path, 5)), expected)

    def test_width_detection_uses_mode_not_first_line(self):
        # One malformed leading record must not set the width for the whole file.
        lines = ["SHORT\r\n"] + [f"{i:020d}\r\n" for i in range(50)]
        path = self.write("".join(lines))
        self.assertEqual(make_command()._detect_record_width(path, APPRAISAL_ENTITY), 20)

    def test_width_detection_rejects_file_narrower_than_layout(self):
        path = self.write("TOOSHORT\r\n" * 10)
        with self.assertRaisesRegex(CommandError, "shorter than"):
            make_command()._detect_record_width(path, APPRAISAL_INFO)

    def test_width_detection_rejects_empty_file(self):
        path = self.write("")
        with self.assertRaisesRegex(CommandError, "no usable records"):
            make_command()._detect_record_width(path, APPRAISAL_ENTITY)


class RowAcceptanceTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "entity.TXT"

    def test_rejects_records_missing_a_natural_key(self):
        # entity_id is the ON CONFLICT target; a blank one cannot be merged.
        self.path.write_text("000000237982C1   \r\n            C2   \r\n", encoding="latin-1")
        rows = list(make_command()._rows(APPRAISAL_ENTITY, self.path, 17))
        self.assertEqual([accepted for _values, accepted in rows], [True, False])

    def test_short_record_is_padded_rather_than_index_erroring(self):
        self.path.write_text("000000237982C1\r\n", encoding="latin-1")
        rows = list(make_command()._rows(APPRAISAL_ENTITY, self.path, 14))
        values, accepted = rows[0]
        self.assertTrue(accepted)
        self.assertEqual(values[0], "237982")


class CleanupTests(SimpleTestCase):
    """Staged files are reclaimed after a successful load, but never blindly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "cad_downloads"
        self.extract_dir = self.data_dir / "extracted" / "2025"
        self.extract_dir.mkdir(parents=True)
        (self.extract_dir / "APPRAISAL_INFO.TXT").write_text("x" * 100)
        self.archive = self.data_dir / "brazos_2025_certified.zip"
        self.archive.write_bytes(b"z" * 50)

    def clean(self, **kwargs):
        options = {"keep_archive": False, "user_supplied_archive": False, **kwargs}
        make_command()._cleanup(self.archive, self.extract_dir, self.data_dir, **options)

    def test_removes_archive_and_extracted_files(self):
        self.clean()
        self.assertFalse(self.archive.exists())
        self.assertFalse(self.extract_dir.exists())

    def test_keep_archive_retains_the_zip_but_drops_extracts(self):
        self.clean(keep_archive=True)
        self.assertTrue(self.archive.exists())
        self.assertFalse(self.extract_dir.exists())

    def test_never_deletes_a_caller_supplied_archive(self):
        # --archive points at a file the user owns; reclaiming it would destroy
        # data the command did not create.
        self.clean(user_supplied_archive=True)
        self.assertTrue(self.archive.exists())
        self.assertFalse(self.extract_dir.exists())

    def test_never_deletes_an_archive_outside_the_staging_directory(self):
        outside = Path(self.tmp.name) / "elsewhere.zip"
        outside.write_bytes(b"z" * 10)
        make_command()._cleanup(
            outside,
            self.extract_dir,
            self.data_dir,
            keep_archive=False,
            user_supplied_archive=False,
        )
        self.assertTrue(outside.exists())

    def test_is_safe_to_run_when_nothing_is_staged(self):
        self.clean()
        self.clean()  # second pass must not raise on already-removed paths


class MergeSqlTests(SimpleTestCase):
    def test_merge_upserts_on_the_natural_key(self):
        sql = make_command()._merge_sql(APPRAISAL_ENTITY, "data_propertyentity", "stg_x")
        self.assertIn('INSERT INTO "data_propertyentity"', sql)
        self.assertIn('ON CONFLICT ("entity_id")', sql)
        self.assertIn("DO UPDATE SET", sql)
        # Non-key columns get refreshed; the key itself must not be reassigned.
        self.assertIn('"entity_cd" = EXCLUDED."entity_cd"', sql)
        self.assertNotIn('"entity_id" = EXCLUDED."entity_id"', sql)

    def test_merge_deduplicates_within_a_batch(self):
        # ON CONFLICT DO UPDATE errors with "cannot affect row a second time" if
        # the source carries duplicate keys, so the SELECT must collapse them.
        sql = make_command()._merge_sql(APPRAISAL_INFO, "data_propertyaccount", "stg_x")
        self.assertIn('SELECT DISTINCT ON ("prop_id", "tax_year")', sql)
        # Last occurrence in file order wins, matching cross-chunk behaviour.
        self.assertIn("_rn DESC", sql)

    def test_merge_sets_timestamps_explicitly(self):
        # COPY bypasses auto_now_add/auto_now, so SQL has to supply them.
        sql = make_command()._merge_sql(APPRAISAL_ENTITY, "data_propertyentity", "stg_x")
        self.assertIn('"created_at", "updated_at"', sql)
        self.assertIn('"updated_at" = now()', sql)
