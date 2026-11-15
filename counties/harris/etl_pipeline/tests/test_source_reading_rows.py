"""The translated rows an HCAD source reading yields per dataset, from temporary trees (#178).

No database: the reading translates the files it selected with the dataset's row translator,
the room counts read from the fixtures member, and the residential account map its caller
supplies.
"""

from pathlib import Path

import pytest

from counties.harris.etl_pipeline.source_reading import (
    HcadDataset,
    read_hcad_source,
    read_hcad_sources,
)
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, HcadSourceId

REAL_ACCOUNT_OWNER = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(HcadSourceId.REAL_ACCOUNT_OWNER)
REAL_BUILDING_LAND = DEFAULT_HCAD_SOURCE_CATALOG.source_for_id(HcadSourceId.REAL_BUILDING_LAND)

BUILDING_HEADER = "acct\tbld_num\timprv_type\theat_ar\n"
FIXTURES_HEADER = "acct\tbld_num\ttype\ttype_dscr\tunits\n"


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="latin-1")
    return root


def _building_land(root: Path, *, fixtures: str | None) -> Path:
    files = {
        "building_res.txt": BUILDING_HEADER + "B100\t1\t1001\t1800\n",
        "extra_features.txt": "acct\tbld_num\tcd\nB100\t1\tGAR\n",
    }
    if fixtures is not None:
        files["fixtures.txt"] = FIXTURES_HEADER + fixtures
    return _write(root, files)


def _rooms(reading, account_map) -> list[tuple]:
    return [
        (row.as_dict()["account_number"], row.as_dict()["bedrooms"], row.as_dict()["bathrooms"])
        for translated in reading.translated_files(HcadDataset.BUILDING)
        for row in translated.rows(account_map)
    ]


def test_buildings_take_room_counts_from_the_catalog_layout_fixtures_member(tmp_path: Path):
    root = _building_land(
        tmp_path,
        fixtures="B100\t1\tRMB\tRoom: Bedroom\t3.00\nB100\t1\tRMF\tRoom: Full Bath\t2.00\n",
    )

    reading = read_hcad_source(REAL_BUILDING_LAND, root)

    assert reading.room_counts().get_stats()["total_buildings"] == 1
    assert _rooms(reading, {"B100": 7}) == [("B100", 3, 2.0)]


def test_without_a_fixtures_member_buildings_have_no_room_counts(tmp_path: Path):
    root = _building_land(tmp_path, fixtures=None)

    reading = read_hcad_source(REAL_BUILDING_LAND, root)

    assert reading.room_counts().get_stats()["total_buildings"] == 0
    assert _rooms(reading, {"B100": 7}) == [("B100", None, None)]
    absent = read_hcad_source(REAL_BUILDING_LAND, tmp_path / "absent")
    assert absent.room_counts().get_stats()["total_buildings"] == 0


def test_room_counts_are_read_once_and_reused_by_every_later_pass(tmp_path: Path):
    root = _building_land(tmp_path, fixtures="B100\t1\tRMB\tRoom: Bedroom\t3.00\n")
    reading = read_hcad_sources([(REAL_BUILDING_LAND, root)])

    reading.read_room_counts()
    (root / "fixtures.txt").unlink()

    # Validation's pass and the load's pass both see the counts of the one read.
    assert _rooms(reading, {"B100": 7}) == [("B100", 3, None)]
    assert _rooms(reading.sources[0], {"B100": 7}) == [("B100", 3, None)]


def test_property_rows_need_no_account_map(tmp_path: Path):
    root = _write(
        tmp_path,
        {
            "real_acct.txt": (
                "acct\tsite_addr_1\tsite_addr_3\tstate_class\ttot_appr_val\n"
                "P100\t1 OAK ST\t77001\tA1\t250000\n"
            )
        },
    )

    reading = read_hcad_source(REAL_ACCOUNT_OWNER, root)

    [translated] = reading.translated_files(HcadDataset.PROPERTY)
    assert translated.path == root / "real_acct.txt"
    assert [row.as_dict()["account_number"] for row in translated.rows()] == ["P100"]


def test_building_and_extra_feature_rows_resolve_accounts_through_the_supplied_map(
    tmp_path: Path,
):
    root = _building_land(tmp_path, fixtures="")
    reading = read_hcad_source(REAL_BUILDING_LAND, root)

    for dataset in (HcadDataset.BUILDING, HcadDataset.EXTRA_FEATURE):
        [translated] = reading.translated_files(dataset)
        [resolved] = translated.rows({"B100": 7})
        [unresolved] = translated.rows({})
        assert resolved.as_dict()["property_id"] == 7
        assert unresolved.invalid
        with pytest.raises(ValueError, match="residential account map"):
            translated.rows()


def test_extra_feature_ordinals_span_the_files_of_one_pass_and_restart_for_the_next(
    tmp_path: Path,
):
    feature = "acct\tbld_num\tcd\tdscr\nB100\t1\tGAR\tGarage\n"
    root = _write(
        tmp_path,
        {
            "building_res.txt": BUILDING_HEADER,
            "fixtures.txt": FIXTURES_HEADER,
            "extra_features_detail_a.txt": feature,
            "extra_features_detail_b.txt": feature,
        },
    )
    reading = read_hcad_sources([(REAL_BUILDING_LAND, root)])

    def ordinals() -> list[tuple[str, object]]:
        return [
            (translated.path.name, row.as_dict()["source_ordinal"])
            for translated in reading.translated_files(HcadDataset.EXTRA_FEATURE)
            for row in translated.rows({"B100": 7})
        ]

    expected = [("extra_features_detail_a.txt", 1), ("extra_features_detail_b.txt", 2)]
    assert ordinals() == expected
    assert ordinals() == expected


def test_room_counts_are_not_a_translated_row_stream(tmp_path: Path):
    reading = read_hcad_source(REAL_BUILDING_LAND, _building_land(tmp_path, fixtures=""))

    with pytest.raises(ValueError, match="not translated"):
        reading.translated_files(HcadDataset.ROOM_COUNTS)
