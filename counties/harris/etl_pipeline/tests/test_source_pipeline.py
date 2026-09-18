"""Unit tests for the deep Harris source pipeline."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from counties.harris.etl_pipeline.config import DataSource, DataSourceType
from counties.harris.etl_pipeline.fixtures_aggregator import FixturesAggregator
from counties.harris.etl_pipeline.source_pipeline import (
    HarrisSourcePipeline,
    resolve_schema_name,
)


def test_resolve_schema_name():
    assert resolve_schema_name("real_acct") == "real_acct"
    assert resolve_schema_name("REAL_ACCT") == "real_acct"
    assert resolve_schema_name("building_res") == "building_res"
    assert resolve_schema_name("extra_features") == "extra_features"
    assert resolve_schema_name("extra_features_detail") == "extra_features"
    assert resolve_schema_name("extra_features_detail_2025") == "extra_features"
    assert resolve_schema_name("desc_building") is None
    assert resolve_schema_name("unknown_table") is None


def test_source_pipeline_missing_extract_path_required(tmp_path: Path):
    source = DataSource(
        name="real_acct",
        url_template="http://example.com/real_acct.zip",
        filename="real_acct.zip",
        source_type=DataSourceType.PROPERTY_DATA,
        required=True,
    )
    extract_manager = MagicMock()
    extract_manager.get_extract_path.return_value = tmp_path / "nonexistent"

    pipeline = HarrisSourcePipeline(
        extract_manager=extract_manager,
        fixtures_aggregator=FixturesAggregator(),
    )

    result = pipeline.process_tabular_source(source, strict=True)
    assert not result.did_work
    assert result.failed == 1
    assert len(result.errors) == 1
    assert "Extract path not found" in result.errors[0]


def test_source_pipeline_missing_extract_path_optional(tmp_path: Path):
    source = DataSource(
        name="optional_source",
        url_template="http://example.com/opt.zip",
        filename="opt.zip",
        source_type=DataSourceType.PROPERTY_DATA,
        required=False,
    )
    extract_manager = MagicMock()
    extract_manager.get_extract_path.return_value = tmp_path / "nonexistent"

    pipeline = HarrisSourcePipeline(
        extract_manager=extract_manager,
        fixtures_aggregator=FixturesAggregator(),
    )

    result = pipeline.process_tabular_source(source, strict=True)
    assert not result.did_work
    assert result.failed == 0
    assert len(result.warnings) == 1
    assert "Extract path not found" in result.warnings[0]


def test_source_pipeline_preload_fixtures_when_file_missing(tmp_path: Path):
    source = DataSource(
        name="real_building_land",
        url_template="http://example.com/Real_building_land.zip",
        filename="Real_building_land.zip",
        source_type=DataSourceType.PROPERTY_DATA,
        required=True,
    )
    extract_dir = tmp_path / "extracted"
    extract_dir.mkdir()
    extract_manager = MagicMock()
    extract_manager.get_extract_path.return_value = extract_dir

    aggregator = FixturesAggregator()
    pipeline = HarrisSourcePipeline(
        extract_manager=extract_manager,
        fixtures_aggregator=aggregator,
    )
    pipeline.preload_fixtures([source])
    assert aggregator.get_stats()["total_buildings"] == 0


@pytest.mark.django_db
def test_source_pipeline_account_map_caching():
    extract_manager = MagicMock()
    pipeline = HarrisSourcePipeline(
        extract_manager=extract_manager,
        fixtures_aggregator=FixturesAggregator(),
    )

    # First access populates cache
    mapping1 = pipeline.get_account_to_property_map()
    assert isinstance(mapping1, dict)

    # Second access returns cached reference
    mapping2 = pipeline.get_account_to_property_map()
    assert mapping1 is mapping2

    # Invalidate clears cache
    pipeline.invalidate_account_map()
    assert pipeline._account_to_property is None
