"""Tests for validate_brazos_against_source's comparison logic.

Only _check_record is unit-tested here with synthetic PropertyAccount rows
-- the command's real job (checking the ~149k-row live-ingested database
against counties/brazos/tests/fixtures/bcad_live_sample_2025.json) can't run
inside manage.py test's isolated/empty test database. Run
`python manage.py validate_brazos_against_source` directly against a
populated database for that.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management import CommandError, call_command
from django.test import TestCase

from counties.brazos.management.commands.validate_brazos_against_source import Command
from counties.brazos.models import PropertyAccount


class CheckRecordTests(TestCase):
    def _account(self, **overrides) -> PropertyAccount:
        defaults = {
            "prop_id": "000000010013",
            "tax_year": 2025,
            "situs_address": "5160 TWIN HILL (PVT) DR",
            "owner_name": "ACOSTA LEON H JR",
            "assessed_value": Decimal("242613"),
        }
        defaults.update(overrides)
        return PropertyAccount(**defaults)

    def _live_record(self, **overrides) -> dict:
        defaults = {
            "prop_id": "000000010013",
            "situs_address_full": "5160 TWIN HILL (PVT) DR BRYAN, TX 77807",
            "owner_name": "ACOSTA LEON H JR",
            "assessed_value": 242613,
        }
        defaults.update(overrides)
        return defaults

    def test_exact_match_has_no_problems(self):
        problems = Command._check_record(self._account(), self._live_record())
        self.assertEqual(problems, [])

    def test_situs_address_must_be_a_prefix_of_the_live_full_address(self):
        account = self._account(situs_address="WRONG ADDRESS")
        problems = Command._check_record(account, self._live_record())
        self.assertEqual(len(problems), 1)
        self.assertIn("situs_address", problems[0])

    def test_blank_situs_address_when_live_has_one_is_flagged(self):
        account = self._account(situs_address="")
        problems = Command._check_record(account, self._live_record())
        self.assertEqual(len(problems), 1)
        self.assertIn("blank", problems[0])

    def test_owner_name_mismatch_is_flagged(self):
        account = self._account(owner_name="SOMEONE ELSE")
        problems = Command._check_record(account, self._live_record())
        self.assertEqual(len(problems), 1)
        self.assertIn("owner_name", problems[0])

    def test_assessed_value_mismatch_is_flagged(self):
        account = self._account(assessed_value=Decimal("100000"))
        problems = Command._check_record(account, self._live_record())
        self.assertEqual(len(problems), 1)
        self.assertIn("assessed_value", problems[0])

    def test_null_assessed_value_against_live_value_is_flagged(self):
        account = self._account(assessed_value=None)
        problems = Command._check_record(account, self._live_record())
        self.assertEqual(len(problems), 1)
        self.assertIn("assessed_value", problems[0])

    def test_multiple_mismatches_all_reported(self):
        account = self._account(owner_name="SOMEONE ELSE", assessed_value=Decimal("1"))
        problems = Command._check_record(account, self._live_record())
        self.assertEqual(len(problems), 2)


class ValidateBrazosAgainstSourceCommandTests(TestCase):
    def test_missing_fixture_raises_command_error(self):
        with self.assertRaises(CommandError):
            call_command("validate_brazos_against_source", "--fixture", "/nonexistent/path.json")

    def test_missing_property_account_row_is_a_failure(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "sample.json"
            fixture.write_text(
                json.dumps(
                    [
                        {
                            "prop_id": "000000099999",
                            "situs_address_full": "1 NOWHERE RD",
                            "owner_name": "NOBODY",
                            "assessed_value": 1,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(CommandError):
                call_command("validate_brazos_against_source", "--fixture", str(fixture))

    def test_all_matching_records_pass(self):
        import json
        import tempfile
        from pathlib import Path

        PropertyAccount.objects.create(
            prop_id="000000010013",
            tax_year=2025,
            situs_address="5160 TWIN HILL (PVT) DR",
            owner_name="ACOSTA LEON H JR",
            assessed_value=Decimal("242613"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "sample.json"
            fixture.write_text(
                json.dumps(
                    [
                        {
                            "prop_id": "000000010013",
                            "situs_address_full": "5160 TWIN HILL (PVT) DR BRYAN, TX 77807",
                            "owner_name": "ACOSTA LEON H JR",
                            "assessed_value": 242613,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            call_command("validate_brazos_against_source", "--fixture", str(fixture))
