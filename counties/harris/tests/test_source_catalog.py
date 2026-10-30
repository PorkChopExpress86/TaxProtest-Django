"""Pure tests for the HCAD source catalog's extracted-layout facts (no Django, no database)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from counties.harris.source_catalog import (
    DEFAULT_HCAD_SOURCE_CATALOG,
    DataSource,
    DataSourceType,
    HcadSourceId,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
PERSISTED_EVIDENCE = REPO_ROOT / "counties" / "common" / "tests" / "persisted_evidence"


def _strip_extension(filename: str) -> str:
    """ExtractManager._get_extract_path's extension-stripping list."""
    for ext in [".zip", ".tar.gz", ".tar", ".gz"]:
        if filename.lower().endswith(ext):
            return filename[: -len(ext)]
    return filename


OLD_STEM_RULES = {
    "extension list": _strip_extension,
    "split on last dot": lambda filename: Path(filename).name.rsplit(".", 1)[0],
    "Path.stem": lambda filename: Path(filename).stem,
    "string replace": lambda filename: filename.replace(".zip", ""),
}


@pytest.mark.parametrize("source_id", list(HcadSourceId))
@pytest.mark.parametrize("rule", sorted(OLD_STEM_RULES))
def test_extract_dirname_equals_every_old_stem_rule(source_id: HcadSourceId, rule: str) -> None:
    filename = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(source_id).filename

    assert DEFAULT_HCAD_SOURCE_CATALOG.extract_dirname(source_id) == OLD_STEM_RULES[rule](filename)


def test_every_source_declares_its_extract_dirname_explicitly() -> None:
    for source in DEFAULT_HCAD_SOURCE_CATALOG.all_sources():
        assert source.extract_dirname, source.source_id


def test_catalog_imports_without_django() -> None:
    """The container build script imports the catalog before Django exists.

    pytest-django has already imported Django in this process, so check a fresh one.
    """
    probe = (
        "import sys, counties.harris.source_catalog; "
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] == 'django'); "
        "sys.exit(f'django modules loaded: {loaded}' if loaded else 0)"
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True, text=True
    )

    assert completed.returncode == 0, completed.stderr


def _persisted_harris_directory_names() -> set[str]:
    names: set[str] = set()

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and "/" in key and not key.startswith("{"):
                    names.add(key.split("/", 1)[0])
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    for path in sorted(PERSISTED_EVIDENCE.glob("harris_*.json")):
        visit(json.loads(path.read_text()))
    return names


def test_persisted_directory_names_are_byte_identical_to_adr_0024_fixtures() -> None:
    persisted = {
        DEFAULT_HCAD_SOURCE_CATALOG.extract_dirname(source_id)
        for source_id in (
            HcadSourceId.REAL_ACCOUNT_OWNER,
            HcadSourceId.REAL_BUILDING_LAND,
            HcadSourceId.GIS_PARCELS,
        )
    }

    assert persisted == {"Real_acct_owner", "Real_building_land", "Parcels"}
    assert persisted == _persisted_harris_directory_names()


# Declared members as (label, patterns, required), written out from today's callers: the
# catalog's required-file table and extract patterns, the jurisdiction-exemption command's
# member constants, and the assessment-history hearing reader.
EXPECTED_MEMBERS: dict[HcadSourceId, tuple[tuple[str, tuple[str, ...], bool], ...]] = {
    HcadSourceId.REAL_ACCOUNT_OWNER: (
        ("real_acct.txt", ("real_acct.txt",), True),
        ("owners.txt", ("owners.txt",), False),
        ("deeds.txt", ("deeds.txt",), False),
    ),
    HcadSourceId.REAL_ACCOUNT_OWNERSHIP_HISTORY: (),
    HcadSourceId.REAL_BUILDING_LAND: (
        ("building_res.txt", ("building_res.txt",), True),
        ("fixtures.txt", ("fixtures.txt",), True),
        ("extra_features*.txt", ("extra_features.txt", "extra_features_detail*.txt"), True),
        ("land.txt", ("land.txt",), False),
    ),
    HcadSourceId.REAL_JUR_EXEMPT: (
        ("jur_value.txt", ("jur_value.txt",), True),
        ("jur_exempt.txt", ("jur_exempt.txt",), True),
        ("jur_tax_dist_exempt_value_rate.txt", ("jur_tax_dist_exempt_value_rate.txt",), True),
        ("jur_exemption_dscr.txt", ("jur_exemption_dscr.txt",), False),
    ),
    HcadSourceId.CODE_DESCRIPTION_REAL: (),
    HcadSourceId.PP_FILES: (),
    HcadSourceId.CODE_DESCRIPTION_PP: (),
    HcadSourceId.HEARING_FILES: (("arb_hearings_real.txt", ("arb_hearings_real.txt",), False),),
    HcadSourceId.GIS_PARCELS: (),
}


@pytest.mark.parametrize("source_id", list(HcadSourceId))
def test_each_source_declares_its_members(source_id: HcadSourceId) -> None:
    declared = tuple(
        (member.label, member.patterns, member.required)
        for member in DEFAULT_HCAD_SOURCE_CATALOG.members(source_id)
    )

    assert declared == EXPECTED_MEMBERS[source_id]


def test_member_names_are_root_relative_file_names() -> None:
    for source_id in HcadSourceId:
        for member in DEFAULT_HCAD_SOURCE_CATALOG.members(source_id):
            for name in (member.label, *member.patterns):
                assert "/" not in name and "\\" not in name


@pytest.mark.parametrize("source_id", list(HcadSourceId))
def test_empty_directory_reports_every_required_member(
    source_id: HcadSourceId, tmp_path: Path
) -> None:
    expected = [label for label, _, required in EXPECTED_MEMBERS[source_id] if required]

    assert DEFAULT_HCAD_SOURCE_CATALOG.missing_required_members(source_id, tmp_path) == expected


def test_jurisdiction_directory_without_optional_description_is_complete(tmp_path: Path) -> None:
    for name in ("jur_value.txt", "jur_exempt.txt", "jur_tax_dist_exempt_value_rate.txt"):
        (tmp_path / name).write_text("")

    missing = DEFAULT_HCAD_SOURCE_CATALOG.missing_required_members(
        HcadSourceId.REAL_JUR_EXEMPT, tmp_path
    )

    assert missing == []


def test_jurisdiction_directory_reports_only_absent_required_members(tmp_path: Path) -> None:
    (tmp_path / "jur_value.txt").write_text("")
    (tmp_path / "jur_exemption_dscr.txt").write_text("")

    missing = DEFAULT_HCAD_SOURCE_CATALOG.missing_required_members(
        HcadSourceId.REAL_JUR_EXEMPT, tmp_path
    )

    assert missing == ["jur_exempt.txt", "jur_tax_dist_exempt_value_rate.txt"]


def test_check_matches_nested_members_case_insensitively(tmp_path: Path) -> None:
    """Same matching as missing_required_files over a recursive listing."""
    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / "Building_Res.txt").write_text("")
    (tmp_path / "fixtures.txt").write_text("")
    (nested / "extra_features_detail1.txt").write_text("")

    missing = DEFAULT_HCAD_SOURCE_CATALOG.missing_required_members(
        HcadSourceId.REAL_BUILDING_LAND, tmp_path
    )

    assert missing == []


def test_absent_directory_reports_every_required_member(tmp_path: Path) -> None:
    missing = DEFAULT_HCAD_SOURCE_CATALOG.missing_required_members(
        HcadSourceId.REAL_ACCOUNT_OWNER, tmp_path / "Real_acct_owner"
    )

    assert missing == ["real_acct.txt"]


# Behaviour callers rely on today, which this expansion must not change.

LEGACY_REQUIRED_FILES = {
    HcadSourceId.REAL_ACCOUNT_OWNER: ["real_acct.txt"],
    HcadSourceId.REAL_BUILDING_LAND: ["building_res.txt", "fixtures.txt", "extra_features*.txt"],
}


@pytest.mark.parametrize("source_id", list(HcadSourceId))
def test_missing_required_files_is_unchanged(source_id: HcadSourceId) -> None:
    source = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(source_id)
    core_files = ["x/real_acct.txt", "x/building_res.txt", "x/fixtures.txt", "x/extra_features.txt"]

    assert DEFAULT_HCAD_SOURCE_CATALOG.missing_required_files(
        source, []
    ) == LEGACY_REQUIRED_FILES.get(source_id, [])
    assert DEFAULT_HCAD_SOURCE_CATALOG.missing_required_files(source, core_files) == []


EXPECTED_EXTRACT_PATTERNS = {
    HcadSourceId.REAL_ACCOUNT_OWNER: ["real_acct.txt", "owners.txt", "deeds.txt"],
    HcadSourceId.REAL_BUILDING_LAND: [
        "building_res.txt",
        "fixtures.txt",
        "extra_features.txt",
        "extra_features_detail*.txt",
        "land.txt",
    ],
}


@pytest.mark.parametrize("source_id", list(HcadSourceId))
def test_extract_patterns_are_unchanged(source_id: HcadSourceId) -> None:
    source = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(source_id)

    assert source.extract_patterns == EXPECTED_EXTRACT_PATTERNS.get(source_id, [])


# Source identity: a source with no catalog id is rejected, never guessed from its names.


def test_a_source_without_a_catalog_id_is_rejected() -> None:
    with pytest.raises(TypeError):
        DataSource(  # type: ignore[call-arg]
            name="Real Account Owner",
            url_template="https://example.com/{year}/Real_acct_owner.zip",
            filename="Real_acct_owner.zip",
            source_type=DataSourceType.PROPERTY_DATA,
        )


@pytest.mark.parametrize("bad_id", [None, "real-account-owner"])
def test_a_source_whose_id_is_not_a_catalog_id_is_rejected(bad_id: object) -> None:
    with pytest.raises(ValueError, match="catalog id"):
        DataSource(
            name="Real Account Owner",
            url_template="https://example.com/{year}/Real_acct_owner.zip",
            filename="Real_acct_owner.zip",
            source_type=DataSourceType.PROPERTY_DATA,
            source_id=bad_id,  # type: ignore[arg-type]
        )


def test_a_legacy_archive_without_a_catalog_id_is_rejected() -> None:
    with pytest.raises(KeyError):
        DEFAULT_HCAD_SOURCE_CATALOG.source_for_archive({"filename": "Real_acct_owner.zip"})


def test_a_legacy_archive_resolves_by_its_catalog_id() -> None:
    source = DEFAULT_HCAD_SOURCE_CATALOG.source_for_archive(
        {"source_id": "gis-parcels", "filename": "anything.zip"}
    )

    assert source.source_id is HcadSourceId.GIS_PARCELS
    assert source.filename == "Parcels.zip"


# Member paths: a declared member, named relative to whichever root holds the extract directory.


def test_member_path_is_root_relative_and_names_the_extract_directory() -> None:
    assert DEFAULT_HCAD_SOURCE_CATALOG.member_path(
        HcadSourceId.REAL_ACCOUNT_OWNER, "real_acct.txt"
    ) == Path("Real_acct_owner/real_acct.txt")
    assert DEFAULT_HCAD_SOURCE_CATALOG.member_path(
        HcadSourceId.HEARING_FILES, "arb_hearings_real.txt"
    ) == Path("Hearing_files/arb_hearings_real.txt")


def test_member_path_rejects_an_undeclared_member() -> None:
    with pytest.raises(LookupError, match="declares no member"):
        DEFAULT_HCAD_SOURCE_CATALOG.member_path(HcadSourceId.REAL_ACCOUNT_OWNER, "fixtures.txt")
