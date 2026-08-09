"""Management command: validate ingested Brazos data against a frozen
snapshot scraped directly from BCAD's own live public property search
(esearch.brazoscad.org), independent of any file this project downloads
in bulk.

Why a frozen fixture instead of a live check: Django's test database is
empty/isolated, so a normal `manage.py test` run has none of the ~149k real
ingested Brazos rows to check against -- there's nothing live to compare.
Re-scraping BCAD on every test run would also make the suite slow, flaky,
and dependent on an external site's availability. Instead,
brazos_cad/tests/fixtures/bcad_live_sample_2025.json is a one-time snapshot
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

A non-zero exit here does not automatically mean a bug: BCAD's live record
can legitimately drift from a frozen certified-export snapshot over time
(a property sale, a post-certification ARB protest correction). In the
initial 50-property sample, 48/50 matched exactly; the 2 that didn't were
each explained by a real, dated event visible on the property's own
"Property Roll Value History"/"Property Deed History" sections (not
re-verified automatically here -- check the live page by hand before
assuming a mismatch is an ingestion bug). A cluster of mismatches, or a
mismatch on situs_address/owner_name (much more stable than dollar values),
is a much stronger signal of a real regression than one or two isolated
assessed_value differences.

Usage:
    python manage.py validate_brazos_against_source
    python manage.py validate_brazos_against_source --verbose
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from brazos_cad.models import PropertyAccount

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

    def handle(self, *args, **options):
        verbose: bool = options["verbose"]
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

            row_failures = self._check_record(account, record)
            if row_failures:
                failures.extend(f"{prop_id}: {f}" for f in row_failures)
                for f in row_failures:
                    self._fail(f"{prop_id}: {f}")
            elif verbose:
                self._pass(f"{prop_id}: situs/owner/assessed_value all match")

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

        self.stdout.write(self.style.SUCCESS(f"  ✅ ALL {checked} PROPERTIES MATCH LIVE BCAD DATA"))
        self.stdout.write("=" * 70 + "\n")

    @staticmethod
    def _check_record(account: PropertyAccount, record: dict) -> list[str]:
        problems: list[str] = []

        expected_full_address = record.get("situs_address_full") or ""
        our_address = (account.situs_address or "").strip()
        if our_address and not expected_full_address.startswith(our_address):
            problems.append(
                f"situs_address {our_address!r} is not a prefix of live "
                f"{expected_full_address!r}"
            )
        elif not our_address and expected_full_address:
            problems.append(f"situs_address is blank, live source has {expected_full_address!r}")

        expected_owner = (record.get("owner_name") or "").strip()
        our_owner = (account.owner_name or "").strip()
        if expected_owner and our_owner != expected_owner:
            problems.append(f"owner_name {our_owner!r} != live {expected_owner!r}")

        expected_assessed = record.get("assessed_value")
        our_assessed = int(account.assessed_value) if account.assessed_value is not None else None
        if expected_assessed is not None and our_assessed != expected_assessed:
            problems.append(f"assessed_value {our_assessed} != live {expected_assessed}")

        return problems

    def _pass(self, msg: str) -> None:
        self.stdout.write(self.style.SUCCESS(f"  ✓ {msg}"))

    def _fail(self, msg: str) -> None:
        self.stdout.write(self.style.ERROR(f"  ✗ {msg}"))
