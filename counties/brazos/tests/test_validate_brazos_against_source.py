"""Tests for validate_brazos_against_source's comparison logic.

Only _check_record is unit-tested here with synthetic PropertyAccount rows
-- the command's real job (checking the ~149k-row live-ingested database
against counties/brazos/tests/fixtures/bcad_live_sample_2025.json) can't run
inside manage.py test's isolated/empty test database. Run
`python manage.py validate_brazos_against_source` directly against a
populated database for that.
"""

from __future__ import annotations

import json
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import TestCase

from counties.brazos.management.commands.validate_brazos_against_source import (
    FIXTURE_PATH,
    Command,
)
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

    def test_problems_are_field_tagged_so_known_drift_can_key_off_them(self):
        account = self._account(assessed_value=Decimal("100000"))
        [(field, message)] = Command._check_record(account, self._live_record())
        self.assertEqual(field, "assessed_value")
        self.assertIn("100000", message)

    def test_situs_address_must_be_a_prefix_of_the_live_full_address(self):
        account = self._account(situs_address="WRONG ADDRESS")
        problems = Command._check_record(account, self._live_record())
        self.assertEqual([field for field, _ in problems], ["situs_address"])

    def test_blank_situs_address_when_live_has_one_is_flagged(self):
        account = self._account(situs_address="")
        problems = Command._check_record(account, self._live_record())
        self.assertEqual([field for field, _ in problems], ["situs_address"])
        self.assertIn("blank", problems[0][1])

    def test_owner_name_mismatch_is_flagged(self):
        account = self._account(owner_name="SOMEONE ELSE")
        problems = Command._check_record(account, self._live_record())
        self.assertEqual([field for field, _ in problems], ["owner_name"])

    def test_assessed_value_mismatch_is_flagged(self):
        account = self._account(assessed_value=Decimal("100000"))
        problems = Command._check_record(account, self._live_record())
        self.assertEqual([field for field, _ in problems], ["assessed_value"])

    def test_null_assessed_value_against_live_value_is_flagged(self):
        account = self._account(assessed_value=None)
        problems = Command._check_record(account, self._live_record())
        self.assertEqual([field for field, _ in problems], ["assessed_value"])

    def test_multiple_mismatches_all_reported(self):
        account = self._account(owner_name="SOMEONE ELSE", assessed_value=Decimal("1"))
        problems = Command._check_record(account, self._live_record())
        self.assertEqual(len(problems), 2)


class ValidateBrazosAgainstSourceCommandTests(TestCase):
    def test_missing_fixture_raises_command_error(self):
        with self.assertRaises(CommandError):
            call_command("validate_brazos_against_source", "--fixture", "/nonexistent/path.json")

    def test_missing_property_account_row_is_a_failure(self):
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


class KnownDriftTests(TestCase):
    """A frozen certified export legitimately drifts from BCAD's live page.

    Marking an investigated field as known_drift keeps the command's exit code
    meaningful: it stays green for drift someone has already explained, and
    goes red the moment a *new* discrepancy shows up.
    """

    def setUp(self):
        PropertyAccount.objects.create(
            prop_id="000000010013",
            tax_year=2025,
            situs_address="5160 TWIN HILL (PVT) DR",
            owner_name="ACOSTA LEON H JR",
            assessed_value=Decimal("242613"),
        )

    def _run(self, record: dict, *args) -> str:
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "sample.json"
            fixture.write_text(json.dumps([record]), encoding="utf-8")
            call_command(
                "validate_brazos_against_source", "--fixture", str(fixture), *args, stdout=out
            )
        return out.getvalue()

    def _record(self, **overrides) -> dict:
        record = {
            "prop_id": "000000010013",
            "situs_address_full": "5160 TWIN HILL (PVT) DR BRYAN, TX 77807",
            "owner_name": "ACOSTA LEON H JR",
            "assessed_value": 242613,
        }
        record.update(overrides)
        return record

    def test_marked_drift_is_reported_but_does_not_fail(self):
        output = self._run(
            self._record(
                assessed_value=999999,
                known_drift={"assessed_value": "post-certification correction"},
            )
        )
        self.assertIn("KNOWN DRIFT ACCEPTED", output)
        self.assertIn("post-certification correction", output)

    def test_strict_turns_known_drift_back_into_a_failure(self):
        with self.assertRaises(CommandError):
            self._run(
                self._record(
                    assessed_value=999999,
                    known_drift={"assessed_value": "post-certification correction"},
                ),
                "--strict",
            )

    def test_drift_marker_only_excuses_the_field_it_names(self):
        """An accepted dollar-value drift must not also silence owner_name."""
        with self.assertRaises(CommandError):
            self._run(
                self._record(
                    assessed_value=999999,
                    owner_name="SOMEONE ELSE ENTIRELY",
                    known_drift={"assessed_value": "post-certification correction"},
                )
            )

    def test_unmarked_mismatch_still_fails(self):
        with self.assertRaises(CommandError):
            self._run(self._record(assessed_value=999999))


class CheckedInFixtureTests(TestCase):
    """Guard the shipped fixture's shape, which the live run depends on."""

    def test_every_known_drift_names_real_fields_and_gives_a_reason(self):
        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        checkable = {"situs_address", "owner_name", "assessed_value"}

        annotated = 0
        for record in records:
            drift = record.get("known_drift")
            if not drift:
                continue
            annotated += 1
            for field, reason in drift.items():
                self.assertIn(field, checkable, f"{record['prop_id']}: unknown field {field!r}")
                self.assertGreater(
                    len(reason), 40, f"{record['prop_id']}: {field} needs a real explanation"
                )

        self.assertEqual(annotated, 2, "the two investigated 2025 drifts should be recorded")
