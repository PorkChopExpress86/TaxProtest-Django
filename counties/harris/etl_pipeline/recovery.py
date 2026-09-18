"""Rebuild Harris property facts from retained catalog inputs."""

from pathlib import Path

from counties.common.import_recovery import ReplayRejected, copy_exact_source
from counties.common.models import ImportCandidate

from .extract import ExtractManager
from .import_plan import HarrisImportPlan


def seed_sources(config, sources, operation):
    retained = operation.evidence["recovery"]["sources"]
    owners = {
        item.get("source_operation_id") or operation.evidence["recovery"]["source_operation_id"]
        for item in retained
        if item.get("source_id") == "real-account-owner"
    }
    for owner in ImportCandidate.objects.filter(county="harris", operation_id__in=owners):
        options = owner.request.get("property_file", {})
        if options.get("append") or options.get("limit") is not None:
            raise ReplayRejected(
                "Automatic recovery is unavailable for append or limited publications; "
                "a reviewed county import of complete source inputs is required"
            )
    manager = ExtractManager(config)
    for source in sources:
        if source.source_id is None:
            raise ReplayRejected("A retained Harris source must have a catalog identity")
        selected = [item for item in retained if item.get("source_id") == source.source_id.value]
        if not selected:
            raise ReplayRejected(f"Retained full Harris inputs are unavailable for {source.name}")
        copied = set()
        root = manager.get_extract_path(source)
        for item in selected:
            original = Path(item["path"])
            if original.suffix.lower() == ".zip":
                destination = config.download_dir / source.filename
            else:
                parent = next(
                    (parent for parent in original.parents if parent.name == root.name), None
                )
                relative = original.relative_to(parent) if parent else Path(original.name)
                destination = root / relative
            if destination not in copied:
                copy_exact_source(item, destination)
                copied.add(destination)
                if original.suffix.lower() == ".zip":
                    operation.evidence.setdefault("acquired_sources", {})[source.filename] = {
                        "source_url": item.get("source_url"),
                        "source_year": item.get("source_year"),
                    }


def prepare_recovery(candidate, replay, *, actor):
    from .orchestrator import (
        HarrisAcquisitionMode,
        HarrisExtractionMode,
        HarrisImportRequest,
        HarrisPrepare,
        run_harris_import,
    )

    # A full replay cannot substitute the current dataset's dependent facts.
    return run_harris_import(
        HarrisImportRequest(
            plan=HarrisImportPlan.from_legacy_scope("full"),
            data_year=candidate.request["data_year"],
            acquisition=HarrisAcquisitionMode.REUSE_DOWNLOADED,
            extraction=HarrisExtractionMode.REUSE_EXTRACTED,
            load=HarrisPrepare(
                validate_completeness=candidate.request.get("validate_completeness", True)
            ),
            actor=actor,
            origin="admin_recovery",
            replay=replay,
        )
    )
