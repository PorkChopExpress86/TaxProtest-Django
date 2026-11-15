"""Unit tests for the deep Harris source pipeline."""

from pathlib import Path

import pytest

from counties.harris.etl_pipeline.config import DataSource, DataSourceType
from counties.harris.etl_pipeline.source_pipeline import HarrisSourcePipeline
from counties.harris.etl_pipeline.source_reading import read_hcad_sources
from counties.harris.source_catalog import HcadSourceId


def test_source_pipeline_missing_extract_path_optional(tmp_path: Path):
    source = DataSource(
        name="optional_source",
        url_template="http://example.com/opt.zip",
        filename="opt.zip",
        source_type=DataSourceType.PROPERTY_DATA,
        source_id=HcadSourceId.PP_FILES,
        required=False,
    )
    pipeline = HarrisSourcePipeline(
        reading=read_hcad_sources([(source, tmp_path / "nonexistent")]),
    )

    result = pipeline.process_tabular_source(source, strict=True)
    assert not result.did_work
    assert result.failed == 0
    assert len(result.warnings) == 1
    assert "Extract path not found" in result.warnings[0]


@pytest.mark.django_db
def test_source_pipeline_account_map_caching():
    pipeline = HarrisSourcePipeline(reading=read_hcad_sources([]))

    # First access populates cache
    mapping1 = pipeline.get_account_to_property_map()
    assert isinstance(mapping1, dict)

    # Second access returns cached reference
    mapping2 = pipeline.get_account_to_property_map()
    assert mapping1 is mapping2

    # Invalidate clears cache
    pipeline.invalidate_account_map()
    assert pipeline._account_to_property is None
