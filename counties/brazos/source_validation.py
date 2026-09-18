"""Inspect exact certified Brazos inputs without publishing any data."""

import hashlib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django.core.management.base import CommandError

from counties.brazos.annual_refresh import StagePreparation, StageResult
from counties.brazos.cad_refresh import CadRefreshStage, _CadStagePayload
from counties.brazos.gis_coordinates import interpret_gis_coordinates
from counties.brazos.gis_refresh import GisSourcePayload
from counties.brazos.parsers import pacs
from counties.common.import_audit import record_sources
from counties.common.models import ImportOperation

_CAD_LAYOUTS = {
    "APPRAISAL_INFO.TXT": (pacs.INFO_LAYOUT, ("prop_id",)),
    "APPRAISAL_LAND_DETAIL.TXT": (pacs.LAND_DETAIL_LAYOUT, ("prop_id", "land_seq")),
    "APPRAISAL_IMPROVEMENT_INFO.TXT": (pacs.IMPROVEMENT_INFO_LAYOUT, ("prop_id", "imp_id")),
    "APPRAISAL_IMPROVEMENT_DETAIL.TXT": (
        pacs.IMPROVEMENT_DETAIL_LAYOUT,
        ("prop_id", "imp_id", "detail_seq"),
    ),
    "APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT": (
        pacs.IMPROVEMENT_DETAIL_ATTR_LAYOUT,
        ("prop_id", "imp_id", "detail_seq", "attribute_type", "attribute_value"),
    ),
    "APPRAISAL_ENTITY_INFO.TXT": (pacs.ENTITY_INFO_LAYOUT, ("prop_id", "tax_unit_code")),
}


def _retain(operation: ImportOperation, preparation: StagePreparation, paths: list[Path]) -> None:
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


def inspect_cad(operation: ImportOperation, preparation: StagePreparation) -> StageResult:
    payload = preparation.payload
    if not isinstance(payload, _CadStagePayload):
        raise CommandError("A validated CAD preview requires inspectable PACS files.")
    _retain(operation, preparation, list(payload.text_files.values()))
    CadRefreshStage().validate_preflight(preparation)
    counts = {}
    duplicates = {}
    for filename, (layout, keys) in _CAD_LAYOUTS.items():
        seen = {}
        count = duplicate = 0
        for number, line in enumerate(CadRefreshStage._iter_lines(payload.text_files[filename]), 1):
            fields = pacs.parse_fixed_width(line, layout)
            prop_id = fields["prop_id"]
            if not prop_id.isdigit() or len(prop_id) != 12:
                raise CommandError(f"Invalid canonical property key in {filename}, row {number}.")
            for spec in layout:
                raw = line[spec.start : spec.end].strip()
                if not raw or spec.cast is pacs._text:
                    continue
                try:
                    numeric = Decimal(raw)
                    if not numeric.is_finite() or (
                        spec.cast in (pacs._int_or_none, pacs._int_or_zero)
                        and numeric != numeric.to_integral_value()
                    ):
                        raise InvalidOperation
                except InvalidOperation as exc:
                    raise CommandError(
                        f"Malformed numeric {spec.name} in {filename}, row {number}."
                    ) from exc
            key = tuple(fields[name] for name in keys)
            fingerprint = hashlib.sha256(repr(fields).encode()).digest()
            if key in seen:
                if seen[key] != fingerprint:
                    raise CommandError(f"Conflicting canonical key in {filename}, row {number}.")
                duplicate += 1
            else:
                seen[key] = fingerprint
            count += 1
        counts[filename] = count
        duplicates[filename] = duplicate
    operation.evidence["cad_inspection"] = {
        "records_by_file": counts,
        "duplicates_by_file": duplicates,
    }
    return StageResult(name="cad", metrics=counts)


def inspect_gis(operation: ImportOperation, preparation: StagePreparation) -> StageResult:
    import geopandas as gpd

    payload = preparation.payload
    if not isinstance(payload, GisSourcePayload) or payload.shapefile_path is None:
        raise CommandError("A validated GIS preview requires a prepared parcel shapefile.")
    paths = sorted(payload.shapefile_path.parent.glob(payload.shapefile_path.stem + ".*"))
    _retain(operation, preparation, paths)
    try:
        frame = gpd.read_file(payload.shapefile_path)
        evidence = interpret_gis_coordinates(frame)
    except Exception as exc:
        raise CommandError(f"Unusable requested GIS: {exc}") from exc
    if not evidence.usable_coordinate_records:
        raise CommandError("Unusable requested GIS: no usable parcel coordinates.")
    if evidence.duplicate_source_ids:
        raise CommandError("Conflicting or repeated canonical GIS property keys.")
    for latitude, longitude in evidence.coordinates.values():
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise CommandError("Unusable requested GIS: coordinates outside geographic bounds.")
    for raw in frame["PROP_ID"]:
        try:
            numeric = Decimal(str(raw))
            if (
                not numeric.is_finite()
                or numeric <= 0
                or numeric >= 10**12
                or numeric != numeric.to_integral_value()
            ):
                raise InvalidOperation
        except InvalidOperation as exc:
            raise CommandError("Invalid canonical GIS property key.") from exc
    return StageResult(
        name="gis",
        metrics={
            "source_records": evidence.source_records,
            "usable_coordinate_records": evidence.usable_coordinate_records,
            "invalid_coordinate_records": evidence.invalid_coordinate_records,
        },
    )
