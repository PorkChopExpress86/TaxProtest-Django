"""Project pins that AGENTS.md states: the mypy target and the Django app labels."""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

from django.apps import apps
from django.test import SimpleTestCase

MYPY_INI = Path(__file__).resolve().parents[2] / "mypy.ini"


class MypyVersionTests(SimpleTestCase):
    def test_mypy_targets_the_running_interpreter(self):
        config = configparser.ConfigParser()
        config.read(MYPY_INI, encoding="utf-8")

        self.assertEqual(
            config["mypy"]["python_version"],
            f"{sys.version_info.major}.{sys.version_info.minor}",
        )


class AppLabelTests(SimpleTestCase):
    """Labels own table names, migration history and content types, so they never follow a move."""

    def test_app_labels_are_pinned_to_their_packages(self):
        self.assertEqual(
            {
                label: apps.get_app_config(label).name
                for label in ("data", "brazos_cad", "counties_common")
            },
            {
                "data": "counties.harris",
                "brazos_cad": "counties.brazos",
                "counties_common": "counties.common",
            },
        )
