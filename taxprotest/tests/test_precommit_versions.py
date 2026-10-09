"""The pre-commit formatters match the dev image's, so a hook never fights the CI gate."""

from __future__ import annotations

import re
from importlib.metadata import version
from pathlib import Path

from django.test import SimpleTestCase

CONFIG = Path(__file__).resolve().parents[2] / ".pre-commit-config.yaml"


def pinned_rev(repo: str) -> str:
    match = re.search(
        rf"repo: https://github\.com/{re.escape(repo)}\s+rev: v?(\S+)",
        CONFIG.read_text(encoding="utf-8"),
    )
    assert match, f"{repo} is not configured in {CONFIG.name}"
    return match.group(1)


class PreCommitVersionTests(SimpleTestCase):
    def test_black_hook_matches_the_installed_black(self):
        self.assertEqual(pinned_rev("psf/black"), version("black"))

    def test_ruff_hook_matches_the_installed_ruff(self):
        self.assertEqual(pinned_rev("astral-sh/ruff-pre-commit"), version("ruff"))
