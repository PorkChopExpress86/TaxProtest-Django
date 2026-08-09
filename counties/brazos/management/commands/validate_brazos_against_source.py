"""Management command: validate ingested Brazos data against a frozen
snapshot scraped directly from BCAD's own live public property search
(esearch.brazoscad.org), independent of any file this project downloads
in bulk.

Why a frozen fixture instead of a live check: Django's test database is
empty/isolated, so a normal `manage.py test` run has none of the ~149k real
ingested Brazos rows to check against -- there's nothing live to compare.
Re-scraping BCAD on every test run would also make the suite slow, flaky,
and dependent on an external site's availability. Instead,
counties/brazos/tests/fixtures/bcad_live_sample_2025.json is a one-time snapshot
(50 real prop_ids, chosen at random from ingested data, scraped via
Playwright -- see the ticket/session notes for the scrape script) that
serves as ground truth: this command re-checks it against whatever's
*currently* in the database, so it catches ingestion regressions without
depending on BCAD's site being reachable at test time.

This is exactly the kind of check that caught a real bug once already: a
first version of this command (an ad-hoc Playwright cross-check, not yet
this command) found that PropertyAccount's GIS-shapefile-derived value
fields were tagged with the wrong tax year -- see wayfinder ticket #3's map
notes and the "Value-field sourcing gotcha" decision entry.

To refresh the fixture (e.g. after ingesting a new tax year): re-run the
Playwright scrape against a fresh random sample of ingested prop_ids and
overwrite the JSON file. Not automated here since it requires a live
Playwright browser, not just Django/the database.

BCAD's live record can legitimately drift from a frozen certified-export
snapshot over time (a property sale, a post-certification ARB protest
correction), so not every mismatch is a bug. In the initial 50-property
sample, 48/50 matched exactly; the 2 that didn't were each explained by a
real, dated event visible on the property's own "Property Roll Value
History"/"Property Deed History" sections.

Those two are recorded in the fixture as ``known_drift`` so this command's
exit code stays meaningful -- green for drift someone has already
investigated, red the moment something *new* appears:

    {"prop_id": "...", ..., "known_drift": {"assessed_value": "why"}}

A marker only excuses the field it names, so an accepted dollar-value drift
never silences that property's owner_name/situs_address checks -- those are
far more stable than dollar values and a mismatch there is a much stronger
regression signal. Investigate a new mismatch against the live page by hand
before either fixing the ETL or adding a marker; ``--strict`` re-fails on
markers so nothing is permanently hidden.

Usage:
    python manage.py validate_brazos_against_source
    python manage.py validate_brazos_against_source --verbose
    python manage.py validate_brazos_against_source --strict
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from counties.brazos.models import PropertyAccount

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "tests"
    / "fixtures"
    / "bcad_live_sample_2025.json"
)


class Command(BaseCommand):
    help = "Validate ingested Brazos data against a frozen live-BCAD-search snapshot."

    def add_arguments(self, parser):
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show a line for every property checked, not just mismatches.",
        )
        parser.add_argument(
            "--fixture",
            default=str(FIXTURE_PATH),
            help="Path to the JSON snapshot (default: the checked-in 50-property sample).",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Also fail on fields marked known_drift in the fixture.",
        )

    def handle(self, *args, **options):
        verbose: bool = options["verbose"]
        strict: bool = options["strict"]
        fixture_path = Path(options["fixture"])

        if not fixture_path.exists():
            raise CommandError(f"Fixture not found: {fixture_path}")

        records = json.loads(fixture_path.read_text(encoding="utf-8"))

        self.stdout.write(self.style.SUCCESS("=" * 70))
        self.stdout.write(self.style.SUCCESS("  BRAZOS DATA VALIDATION AGAINST LIVE BCAD SOURCE"))
        self.stdout.write(
            self.style.SUCCESS(f"  ({len(records)} properties, from {fixture_path.name})")
        )
        self.stdout.write(self.style.SUCCESS("=" * 70))

        failures: list[str] = []
        drifts: list[str] = []
        missing = 0

        for record in records:
            prop_id = record["prop_id"]
            account = PropertyAccount.objects.filter(prop_id=prop_id, tax_year=2025).first()

            if account is None:
                missing += 1
                msg = f"{prop_id}: no PropertyAccount row for tax_year=2025"
                self._fail(msg)
                failures.append(msg)
                continue

            known_drift = record.get("known_drift") or {}
            problems = self._check_record(account, record)
            if not problems:
                if verbose:
                    self._pass(f"{prop_id}: situs/owner/assessed_value all match")
                continue

            for field, message in problems:
                reason = known_drift.get(field)
                if reason is not None and not strict:
                    drifts.append(f"{prop_id}: {message}")
                    self._drift(f"{prop_id}: {message}  [known: {reason}]")
                else:
                    failures.append(f"{prop_id}: {message}")
                    self._fail(f"{prop_id}: {message}")

        self.stdout.write("\n" + "=" * 70)
        checked = len(records) - missing
        if failures:
            self.stdout.write(
                self.style.ERROR(
                    f"  VALIDATION FAILED — {len(failures)} issue(s) across "
                    f"{len(records)} properties ({checked} found, {missing} missing)"
                )
            )
            self.stdout.write("=" * 70 + "\n")
            raise CommandError(f"{len(failures)} mismatch(es) against live BCAD data")

        summary = f"  ✅ ALL {checked} PROPERTIES MATCH LIVE BCAD DATA"
        if drifts:
            summary = (
                f"  ✅ {checked - len(drifts)} MATCH, {len(drifts)} KNOWN DRIFT ACCEPTED "
                f"(re-run with --strict to fail on those too)"
            )
        self.stdout.write(self.style.SUCCESS(summary))
        self.stdout.write("=" * 70 + "\n")

    @staticmethod
    def _check_record(account: PropertyAccount, record: dict) -> list[tuple[str, str]]:
        """Return (field_name, message) per mismatch.

        The field name is what ``known_drift`` keys off, so a property whose
        dollar value has legitimately moved since certification can be accepted
        without also silencing its owner/address checks.
        """
        problems: list[tuple[str, str]] = []

        expected_full_address = record.get("situs_address_full") or ""
        our_address = (account.situs_address or "").strip()
        if our_address and not expected_full_address.startswith(our_address):
            problems.append(
                (
                    "situs_address",
                    f"situs_address {our_address!r} is not a prefix of live "
                    f"{expected_full_address!r}",
                )
            )
        elif not our_address and expected_full_address:
            problems.append(
                (
                    "situs_address",
                    f"situs_address is blank, live source has {expected_full_address!r}",
                )
            )

        expected_owner = (record.get("owner_name") or "").strip()
        our_owner = (account.owner_name or "").strip()
        if expected_owner and our_owner != expected_owner:
            problems.append(("owner_name", f"owner_name {our_owner!r} != live {expected_owner!r}"))

        expected_assessed = record.get("assessed_value")
        our_assessed = int(account.assessed_value) if account.assessed_value is not None else None
        if expected_assessed is not None and our_assessed != expected_assessed:
            problems.append(
                (
                    "assessed_value",
                    f"assessed_value {our_assessed} != live {expected_assessed}",
                )
            )

        return problems

    def _pass(self, msg: str) -> None:
        self.stdout.write(self.style.SUCCESS(f"  ✓ {msg}"))

    def _fail(self, msg: str) -> None:
        self.stdout.write(self.style.ERROR(f"  ✗ {msg}"))

    def _drift(self, msg: str) -> None:
        self.stdout.write(self.style.WARNING(f"  ~ {msg}"))
