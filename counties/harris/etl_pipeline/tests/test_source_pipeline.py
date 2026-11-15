"""Unit tests for the deep Harris source pipeline."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from counties.harris.etl_pipeline.config import DataSource, DataSourceType
from counties.harris.etl_pipeline.fixtures_aggregator import FixturesAggregator
from counties.harris.etl_pipeline.source_pipeline import HarrisSourcePipeline
from counties.harris.source_catalog import HcadSourceId


def test_source_pipeline_missing_extract_path_required(tmp_path: Path):
    source = DataSource(
        name="real_acct",
        url_template="http://example.com/real_acct.zip",
        filename="real_acct.zip",
        source_type=DataSourceType.PROPERTY_DATA,
        source_id=HcadSourceId.REAL_ACCOUNT_OWNER,
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
        source_id=HcadSourceId.PP_FILES,
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
