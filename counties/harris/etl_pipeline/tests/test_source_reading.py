"""Which extracted HCAD files feed which dataset, read from temporary extract trees (#177).

No database: the HCAD source reading only looks at the files under each source's extract
directory and the HCAD source catalog's declared members.
"""

from pathlib import Path

from counties.harris.etl_pipeline.source_reading import (
    HcadDataset,
    read_hcad_source,
    read_hcad_sources,
)
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, HcadSourceId

REAL_ACCOUNT_OWNER = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(HcadSourceId.REAL_ACCOUNT_OWNER)
REAL_BUILDING_LAND = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(HcadSourceId.REAL_BUILDING_LAND)


def _tree(root: Path, *names: str) -> Path:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("acct\n")
    return root


def _names(paths: tuple[Path, ...], root: Path) -> list[str]:
    return [path.relative_to(root).as_posix() for path in paths]


def test_detail_files_supersede_the_combined_extra_features_file(tmp_path: Path):
    root = _tree(
        tmp_path,
        "building_res.txt",
        "extra_features.txt",
        "extra_features_detail1.txt",
        "extra_features_detail2.txt",
        "fixtures.txt",
    )

    reading = read_hcad_source(REAL_BUILDING_LAND, root)

    assert _names(reading.files(HcadDataset.EXTRA_FEATURE), root) == [
        "extra_features_detail1.txt",
        "extra_features_detail2.txt",
    ]


def test_the_combined_extra_features_file_feeds_extra_features_without_detail_files(
    tmp_path: Path,
):
    root = _tree(tmp_path, "building_res.txt", "extra_features.txt", "fixtures.txt")

    reading = read_hcad_source(REAL_BUILDING_LAND, root)

    assert _names(reading.files(HcadDataset.EXTRA_FEATURE), root) == ["extra_features.txt"]


def test_a_stray_extra_features_file_is_ignored(tmp_path: Path):
    root = _tree(
        tmp_path, "building_res.txt", "extra_features.txt", "extra_features_old.txt", "fixtures.txt"
    )

    reading = read_hcad_source(REAL_BUILDING_LAND, root)

    assert _names(reading.files(HcadDataset.EXTRA_FEATURE), root) == ["extra_features.txt"]
    assert "extra_features_old.txt" not in _names(
        tuple(member.path for member in reading.members), root
    )


def test_members_nested_in_a_subfolder_are_found(tmp_path: Path):
    property_root = _tree(tmp_path / "Real_acct_owner", "2026/real_acct.txt", "2026/owners.txt")
    building_root = _tree(
        tmp_path / "Real_building_land",
        "2026/building_res.txt",
        "2026/fixtures.txt",
        "2026/extra_features_detail1.txt",
        "land.txt",
    )

    reading = read_hcad_sources(
        [(REAL_ACCOUNT_OWNER, property_root), (REAL_BUILDING_LAND, building_root)]
    )

    assert _names(reading.files(HcadDataset.PROPERTY), tmp_path) == [
        "Real_acct_owner/2026/real_acct.txt"
    ]
    assert _names(reading.files(HcadDataset.BUILDING), tmp_path) == [
        "Real_building_land/2026/building_res.txt"
    ]
    assert _names(reading.files(HcadDataset.EXTRA_FEATURE), tmp_path) == [
        "Real_building_land/2026/extra_features_detail1.txt"
    ]
    assert _names(reading.files(HcadDataset.ROOM_COUNTS), tmp_path) == [
        "Real_building_land/2026/fixtures.txt"
    ]
    assert [source.missing_required_members for source in reading.sources] == [(), ()]


def test_room_counts_come_only_from_the_building_land_source(tmp_path: Path):
    property_root = _tree(tmp_path / "Real_acct_owner", "real_acct.txt", "fixtures.txt")
    building_root = _tree(
        tmp_path / "Real_building_land", "building_res.txt", "extra_features.txt", "fixtures.txt"
    )

    reading = read_hcad_sources(
        [(REAL_ACCOUNT_OWNER, property_root), (REAL_BUILDING_LAND, building_root)]
    )

    assert _names(reading.files(HcadDataset.ROOM_COUNTS), tmp_path) == [
        "Real_building_land/fixtures.txt"
    ]


def test_each_member_is_read_only_from_the_source_that_declares_it(tmp_path: Path):
    property_root = _tree(
        tmp_path / "Real_acct_owner", "real_acct.txt", "building_res.txt", "extra_features.txt"
    )
    building_root = _tree(
        tmp_path / "Real_building_land",
        "real_acct.txt",
        "building_res.txt",
        "extra_features.txt",
        "fixtures.txt",
    )

    reading = read_hcad_sources(
        [(REAL_ACCOUNT_OWNER, property_root), (REAL_BUILDING_LAND, building_root)]
    )

    assert _names(reading.files(HcadDataset.PROPERTY), tmp_path) == [
        "Real_acct_owner/real_acct.txt"
    ]
    assert _names(reading.files(HcadDataset.BUILDING), tmp_path) == [
        "Real_building_land/building_res.txt"
    ]
    assert _names(reading.files(HcadDataset.EXTRA_FEATURE), tmp_path) == [
        "Real_building_land/extra_features.txt"
    ]


def test_members_are_read_in_path_order_across_datasets(tmp_path: Path):
    root = _tree(
        tmp_path,
        "land.txt",
        "fixtures.txt",
        "extra_features_detail1.txt",
        "extra_features.txt",
        "building_res.txt",
    )

    reading = read_hcad_source(REAL_BUILDING_LAND, root)

    assert [(member.path.name, member.dataset) for member in reading.members] == [
        ("building_res.txt", HcadDataset.BUILDING),
        ("extra_features_detail1.txt", HcadDataset.EXTRA_FEATURE),
        ("fixtures.txt", HcadDataset.ROOM_COUNTS),
    ]


def test_a_required_source_with_a_missing_required_member_is_reported(tmp_path: Path):
    root = _tree(tmp_path, "building_res.txt", "extra_features_old.txt", "land.txt")

    reading = read_hcad_source(REAL_BUILDING_LAND, root)

    assert reading.missing_required_members == ("fixtures.txt", "extra_features*.txt")
    assert _names(reading.files(HcadDataset.BUILDING), root) == ["building_res.txt"]


def test_a_missing_extract_directory_reports_every_required_member(tmp_path: Path):
    reading = read_hcad_source(REAL_ACCOUNT_OWNER, tmp_path / "absent")

    assert reading.members == ()
    assert reading.missing_required_members == ("real_acct.txt",)


def test_an_optional_source_reports_no_missing_members(tmp_path: Path):
    jur_exempt = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(HcadSourceId.REAL_JUR_EXEMPT)

    reading = read_hcad_source(jur_exempt, tmp_path)

    assert reading.missing_required_members == ()
