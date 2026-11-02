"""Retain the exact certified Brazos inputs an inspection selected, without publishing data.

The CAD and GIS stage adapters own their inspections; this module owns only the shared
step that records each selected file as an import source and checks it against the
retained archive it came from.
"""

import hashlib
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django.core.management.base import CommandError

from counties.brazos.stage_contract import StagePreparation
from counties.common.import_audit import record_sources
from counties.common.models import ImportOperation


def retain_sources(
    operation: ImportOperation, preparation: StagePreparation, paths: list[Path]
) -> None:
    payload = preparation.payload
    archive = payload.archive
    if archive and archive.is_file():
        paths = [archive, *paths]
    record_sources(
        operation,
        paths,
        stage=preparation.name,
        source_year=preparation.source_year,
        target_year=preparation.target_year,
        source_url=payload.source_url or None,
    )
    if not archive or not archive.is_file():
        return
    try:
        with ZipFile(archive) as zipped:
            if zipped.testzip():
                raise CommandError(f"Invalid archive integrity: {archive.name}")
            for path in paths:
                if path == archive:
                    continue
                # CAD names may carry a timestamp prefix; consume the exact selected basename.
                names = [
                    name
                    for name in zipped.namelist()
                    if Path(name.replace("\\", "/")).name == path.name
                ]
                if len(names) != 1:
                    raise CommandError(
                        f"Cannot attribute selected input {path.name} to retained archive."
                    )
                digest = hashlib.sha256()
                with zipped.open(names[0]) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                expected = next(
                    source["sha256"]
                    for source in operation.evidence["sources"]
                    if source["path"] == str(path)
                )
                if digest.hexdigest() != expected:
                    raise CommandError(f"Selected input {path.name} differs from retained archive.")
    except BadZipFile as exc:
        raise CommandError(f"Invalid source archive: {archive.name}") from exc
