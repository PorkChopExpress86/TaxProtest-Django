"""County registration: the one declaration each county makes for shared Import code (ADR-0022)."""

from pathlib import Path

from django.test import SimpleTestCase

from counties.brazos.candidate import BrazosCandidatePort
from counties.common.county_registry import (
    CountyRegistration,
    InvalidRegistration,
    UnknownCounty,
    isolated_registrations,
    register,
    registered_slugs,
    registration_for,
)
from counties.harris.etl_pipeline.candidate import HarrisCandidatePort


class CountyRegistrationShape:
    slug: str
    port_type: type
    lock_key: int
    warning_logger: str
    settings_prefix: str

    def test_registers_its_candidate_port_and_county_facts(self):
        registration = registration_for(self.slug)

        self.assertEqual(registration.slug, self.slug)
        self.assertIsInstance(registration.port, self.port_type)
        self.assertEqual(registration.writer_lock_key, self.lock_key)
        self.assertEqual(registration.warning_logger, self.warning_logger)

    def test_source_roots_are_read_from_settings_at_call_time(self):
        with self.settings(
            **{
                f"{self.settings_prefix}_DOWNLOAD_DIR": "/managed/downloads",
                f"{self.settings_prefix}_EXTRACT_DIR": "/managed/extracts",
            }
        ):
            roots = registration_for(self.slug).source_roots()

        self.assertEqual(roots, (Path("/managed/downloads"), Path("/managed/extracts")))


class HarrisRegistrationTests(CountyRegistrationShape, SimpleTestCase):
    slug = "harris"
    port_type = HarrisCandidatePort
    lock_key = 742101
    warning_logger = "etl_orchestrator"
    settings_prefix = "HCAD"


class BrazosRegistrationTests(CountyRegistrationShape, SimpleTestCase):
    slug = "brazos"
    port_type = BrazosCandidatePort
    lock_key = 742102
    warning_logger = "brazos_cad"
    settings_prefix = "BCAD"


class RegistryTests(SimpleTestCase):
    def test_both_counties_register_from_their_app_configuration(self):
        self.assertEqual(registered_slugs(), ("brazos", "harris"))

    def test_an_unregistered_county_is_unknown(self):
        with self.assertRaises(UnknownCounty):
            registration_for("travis")

    def test_a_lock_key_already_held_by_another_county_is_rejected(self):
        harris = registration_for("harris")
        with isolated_registrations():
            with self.assertRaises(InvalidRegistration):
                register(_registration(slug="travis", writer_lock_key=harris.writer_lock_key))

            self.assertEqual(registered_slugs(), ("brazos", "harris"))

    def test_a_valid_new_county_registers_without_shared_code_edits(self):
        with isolated_registrations():
            register(_registration(slug="travis", writer_lock_key=742199))

            self.assertEqual(registration_for("travis").writer_lock_key, 742199)
        self.assertEqual(registered_slugs(), ("brazos", "harris"))


def _registration(**overrides):
    fields = {
        "slug": "travis",
        "port": registration_for("harris").port,
        "writer_lock_key": 742199,
        "warning_logger": "travis_cad",
        "source_roots": lambda: (Path("/downloads"), Path("/extracts")),
        **overrides,
    }
    return CountyRegistration(**fields)


class MalformedRegistrationTests(SimpleTestCase):
    def test_malformed_registrations_are_rejected_when_declared(self):
        malformed = {
            "empty slug": {"slug": ""},
            "uppercase slug": {"slug": "Travis"},
            "slug longer than a county column": {"slug": "a" * 17},
            "port without the candidate questions": {"port": object()},
            "lock key as text": {"writer_lock_key": "742199"},
            "lock key as a boolean": {"writer_lock_key": True},
            "non-positive lock key": {"writer_lock_key": 0},
            "lock key outside the advisory range": {"writer_lock_key": 2**31},
            "empty warning logger": {"warning_logger": ""},
            "source roots as a path, not a reader": {"source_roots": "/downloads"},
        }
        for case, overrides in malformed.items():
            with self.subTest(case), self.assertRaises(InvalidRegistration):
                _registration(**overrides)

    def test_only_a_registration_can_be_registered(self):
        with isolated_registrations(), self.assertRaises(InvalidRegistration):
            register(registration_for("harris").port)
