"""Parity test: fast_loader and transform must produce identical rows for
the same HCAD input, including inputs with embedded quote characters.

Both readers use ``csv.QUOTE_NONE`` (see ``fast_loader._open_text`` and
``transform.DataTransformer._open_reader``). This test locks that
equivalence so a future change to one reader's quoting policy can't
silently diverge from the other.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from counties.harris.etl_pipeline.config import ETLConfig
from counties.harris.etl_pipeline.row_reader import _open_text
from counties.harris.etl_pipeline.transform import DataTransformer


class QuotingParityTests(SimpleTestCase):
    """Both readers must handle embedded quotes identically."""

    def _write_hcad_file(self, content: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="latin-1", suffix=".txt")
        tmp.write(content)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return Path(tmp.name)

    def test_embedded_quote_does_not_corrupt_rows(self) -> None:
        """A field containing an unbalanced " must not swallow the next row."""
        header = "acct\tstr_num\tstr\tsite_addr_1\tsite_addr_3\ttot_appr_val\tstate_class"
        rows = [
            # Normal row
            "R1\t100\tMAIN ST\t100 MAIN ST\t77001\t250000\tA1",
            # Row with embedded quote in address (HCAD free-text fields do this)
            'R2\t200\tOAK "AVE"\t200 OAK "AVE"\t77002\t350000\tA2',
            # Another normal row that default-quoting would have swallowed
            "R3\t300\tELM ST\t300 ELM ST\t77003\t450000\tA1",
        ]
        content = header + "\n" + "\n".join(rows) + "\n"
        path = self._write_hcad_file(content)

        # fast_loader path
        fast_reader, fast_fh = _open_text(path)
        fast_rows = list(fast_reader)
        fast_fh.close()

        # transform path — need a minimal config with a temp dir
        with tempfile.TemporaryDirectory() as tmpdir:
            config = ETLConfig(
                download_dir=Path(tmpdir) / "d",
                extract_dir=Path(tmpdir) / "e",
                log_dir=Path(tmpdir) / "l",
            )
            transformer = DataTransformer(config)

            transform_reader, transform_fh = transformer.open_reader(path)
            transform_rows = list(transform_reader)
            transform_fh.close()

        # fast_loader uses csv.reader (positional, header is row 0)
        # transform uses DictReader (header consumed automatically)
        # Both must see exactly 3 data rows with the embedded quote intact
        self.assertEqual(len(fast_rows), 4)  # header + 3 data
        self.assertEqual(len(transform_rows), 3)  # DictReader skips header

        # fast_loader: positional reader, rows are lists (row 0 = header)
        #   row 1 = R1, row 2 = R2, row 3 = R3
        # transform: DictReader, rows are dicts keyed by header (no header row)
        #   row 0 = R1, row 1 = R2, row 2 = R3
        # Verify the embedded-quote row (R2) parses identically
        self.assertEqual(fast_rows[2][0], "R2")
        self.assertEqual(fast_rows[2][2], 'OAK "AVE"')
        self.assertEqual(fast_rows[2][3], '200 OAK "AVE"')

        self.assertEqual(transform_rows[1]["acct"], "R2")
        self.assertEqual(transform_rows[1]["str"], 'OAK "AVE"')
        self.assertEqual(transform_rows[1]["site_addr_1"], '200 OAK "AVE"')

        # The third data row (R3) is not swallowed by the quote
        self.assertEqual(fast_rows[3][0], "R3")
        self.assertEqual(transform_rows[2]["acct"], "R3")
