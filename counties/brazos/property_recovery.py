"""Retained CAD and GIS replay through the county's actual source ports."""

from pathlib import Path

from django.conf import settings

from counties.brazos.cad_refresh import ALL_FILENAMES, CadRefreshStage, _CadStagePayload
from counties.brazos.gis_refresh import GisRefreshStage, GisSourcePayload
from counties.brazos.property_import import (
    BrazosPropertyImport,
    PropertyImportMode,
    PropertyImportRequest,
    RefreshOptions,
    StagePreparation,
)
from counties.common.import_recovery import ReplayRejected, copy_exact_source
from counties.common.import_writers import working_source_root


class RetainedCadStage(CadRefreshStage):
    def __init__(self, sources):
        super().__init__()
        self.sources = [item for item in sources if item.get("stage") == "cad"]

    def prepare(self, options):
        if not self.sources:
            raise ReplayRejected("Retained CAD sources are unavailable")
        year = self.sources[0]["source_year"]
        root = working_source_root(Path(settings.BCAD_EXTRACT_DIR), reuse=False) / str(year)
        files = {}
        for filename in ALL_FILENAMES:
            item = next(
                (
                    item
                    for item in self.sources
                    if Path(item["path"]).name.upper().endswith(filename)
                ),
                None,
            )
            if item is None:
                raise ReplayRejected(f"Retained CAD input unavailable: {filename}")
            destination = root / Path(item["path"]).name
            copy_exact_source(item, destination)
            files[filename] = destination
        archive = _copy_archive(self.sources)
        return StagePreparation(
            "cad",
            year,
            options.tax_year or year,
            _CadStagePayload(files, root, archive, self.sources[0].get("source_url") or ""),
            (),
        )


def _copy_archive(sources):
    item = next((item for item in sources if Path(item["path"]).suffix.lower() == ".zip"), None)
    if item is None:
        return None
    destination = (
        working_source_root(Path(settings.BCAD_DOWNLOAD_DIR), reuse=False) / Path(item["path"]).name
    )
    copy_exact_source(item, destination)
    return destination


class RetainedGisStage(GisRefreshStage):
    def __init__(self, sources):
        super().__init__()
        self.sources = [item for item in sources if item.get("stage") == "gis"]

    def prepare(self, options):
        shape = next(
            (item for item in self.sources if Path(item["path"]).suffix.lower() == ".shp"), None
        )
        if shape is None:
            raise ReplayRejected("Retained GIS layer unavailable")
        original = Path(shape["path"])
        year = shape["source_year"]
        root = working_source_root(Path(settings.BCAD_EXTRACT_DIR), reuse=False) / "gis" / str(year)
        for item in self.sources:
            path = Path(item["path"])
            if path.parent == original.parent and path.stem == original.stem:
                copy_exact_source(item, root / path.name)
        archive = _copy_archive(self.sources)
        return StagePreparation(
            "gis",
            year,
            options.tax_year or year,
            GisSourcePayload(root / original.name, root, archive, shape.get("source_url") or ""),
            (),
        )


def prepare_recovery(candidate, replay, *, actor):
    partial = candidate.evidence["outcome"] == "partial"
    return BrazosPropertyImport(
        RetainedCadStage(candidate.sources),
        None if partial else RetainedGisStage(candidate.sources),
    ).run(
        PropertyImportRequest(
            mode=PropertyImportMode.CAD_RECOVERY if partial else PropertyImportMode.ANNUAL,
            options=RefreshOptions(
                tax_year=candidate.request["tax_year"], skip_download=True, skip_extract=True
            ),
            prepare_only=True,
            actor=actor,
            origin="admin_recovery",
            replay=replay,
        )
    )
