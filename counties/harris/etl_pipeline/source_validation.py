"""County-owned inspection of the exact selected Harris property inputs."""

import csv
import hashlib
import math
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from counties.common.import_audit import record_sources
from counties.common.models import ImportOperation
from counties.harris.models import PropertyRecord
from counties.harris.source_catalog import DEFAULT_HCAD_SOURCE_CATALOG, DataSource, DataSourceType

from .config import ETLConfig
from .extract import ExtractManager
from .fixtures_aggregator import FixturesAggregator
from .gis_loader import inspect_gis_parcels, select_preferred_gis_shapefile
from .row_reader import (
    BUILDING_RES_SOURCES,
    EXTRA_FEATURES_SOURCES,
    REAL_ACCT_SOURCES,
    iter_building_rows,
    iter_extra_feature_rows,
    iter_property_rows,
)


@dataclass
class HarrisSourceValidation:
    errors: list[str] = field(default_factory=list)
    translated: dict[str, dict[str, int]] = field(default_factory=dict)
    account_map: dict[str, int] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    fixture_units: dict[tuple[str, int, str], Decimal] = field(default_factory=dict)

    def evidence(self):
        return {
            "valid": not self.errors,
            "errors": self.errors,
            "files": self.files,
            "translated": self.translated,
            "database_publication": "Database publication untested",
        }

    def metrics(self):
        return {
            "records_loaded": sum(
                counts["loaded"] for schema, counts in self.translated.items() if schema != "gis"
            ),
            "records_invalid": sum(counts["invalid"] for counts in self.translated.values()),
            "records_skipped": sum(counts["skipped"] for counts in self.translated.values()),
            "records_failed": 0,
            "gis_coordinates_updated": self.translated.get("gis", {}).get("loaded", 0),
            "_sources_succeeded": 1 if not self.errors else 0,
            "_wrote_data": False,
        }


_LAYOUTS = {
    "real_acct": (REAL_ACCT_SOURCES, ("account_number", "state_class")),
    "building_res": (BUILDING_RES_SOURCES, ("account_number", "building_number")),
    "extra_features": (
        EXTRA_FEATURES_SOURCES,
        ("account_number", "feature_number", "feature_code"),
    ),
    "fixtures": (
        {name: [name] for name in ("acct", "bld_num", "type", "units")},
        ("acct", "bld_num", "type", "units"),
    ),
}
_TEXT_FIELDS = {
    "account_number",
    "owner_name",
    "street_number",
    "street_name",
    "street_suffix",
    "site_addr_1",
    "city",
    "zipcode",
    "state_class",
    "building_type",
    "building_style",
    "building_class",
    "quality_code",
    "condition_code",
    "foundation_type",
    "exterior_wall",
    "roof_cover",
    "roof_type",
    "feature_code",
    "feature_description",
    "acct",
    "type",
}


def _check_archive_inputs(
    archive: Path,
    paths: list[Path],
    operation: ImportOperation,
    result: HarrisSourceValidation,
    *,
    relative_to: Path | None = None,
) -> None:
    if not archive.exists():
        return
    expected = {
        source["path"]: source["sha256"] for source in operation.evidence.get("sources", [])
    }
    try:
        with zipfile.ZipFile(archive) as contents:
            for path in paths:
                if not path.is_file():
                    continue
                suffix = (
                    str(path.relative_to(relative_to) if relative_to else path.name)
                    .replace("\\", "/")
                    .lower()
                )
                members = [
                    name
                    for name in contents.namelist()
                    if name.replace("\\", "/").lower() == suffix
                    or name.replace("\\", "/").lower().endswith("/" + suffix)
                ]
                matched = False
                for name in members:
                    digest = hashlib.sha256()
                    with contents.open(name) as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest() == expected[str(path)]:
                        matched = True
                if not matched:
                    result.errors.append(
                        f"{path.name}: extracted content does not match the retained archive"
                    )
    except (OSError, zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        result.errors.append(f"{archive.name}: extracted-source attribution unavailable: {exc}")


def _inspect_layout(path: Path, schema: str, year: int, result: HarrisSourceValidation) -> None:
    aliases, required = _LAYOUTS[schema]
    count = invalid = 0
    years: set[int] = set()
    with path.open(encoding="latin-1", newline="") as stream:
        reader = csv.reader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
        header = next(reader, [])
        columns = [name.strip().lower() for name in header]
        indices = {
            field: next(
                (columns.index(name.lower()) for name in candidates if name.lower() in columns),
                None,
            )
            for field, candidates in aliases.items()
        }
        missing = [field for field in required if indices[field] is None]
        if missing or len(set(columns)) != len(columns):
            result.errors.append(
                f"{path.name}: invalid layout; missing {', '.join(missing)} or duplicate columns"
            )
        year_index = next(
            (columns.index(name) for name in ("yr", "tax_year") if name in columns), None
        )
        for number, row in enumerate(reader, 2):
            if not row or not any(value.strip() for value in row):
                continue
            count += 1
            problems = []
            if len(row) != len(header):
                problems.append("row does not match header layout")
            else:
                account_field = "acct" if schema == "fixtures" else "account_number"
                account_index = indices.get(account_field)
                account = row[account_index].strip() if account_index is not None else ""
                if not account or len(account) > 20:
                    problems.append("missing or overlong canonical account identity")
                if schema == "fixtures" and any(
                    indices[field] is not None and not row[indices[field]].strip()
                    for field in required
                ):
                    problems.append("missing canonical fixture fields")
                for field_name, index in indices.items():
                    if index is None or field_name in _TEXT_FIELDS or not row[index].strip():
                        continue
                    try:
                        raw_value = row[index].strip()
                        value = Decimal(
                            raw_value
                            if field_name in ("building_number", "feature_number", "bld_num")
                            else raw_value.replace("$", "").replace(",", "")
                        )
                        if not value.is_finite() or not math.isfinite(float(value)):
                            raise InvalidOperation
                        if field_name in ("building_number", "feature_number", "bld_num") and (
                            value != value.to_integral_value() or value < 1
                        ):
                            raise InvalidOperation
                        if field_name == "bld_num":
                            int(raw_value)  # FixturesAggregator uses integer lexical parsing.
                    except (InvalidOperation, ValueError):
                        problems.append(f"invalid numeric {field_name}")
                if year_index is not None:
                    try:
                        actual_year = int(row[year_index].strip())
                        years.add(actual_year)
                        if actual_year != year:
                            problems.append(
                                f"source year {actual_year} differs from requested {year}"
                            )
                    except ValueError:
                        problems.append("invalid source year")
                if schema == "fixtures" and not problems and not missing:
                    key = (
                        account,
                        int(Decimal(row[indices["bld_num"]])),
                        row[indices["type"]].strip(),
                    )
                    units = Decimal(row[indices["units"]])
                    previous = result.fixture_units.get(key)
                    if previous is not None and previous != units:
                        problems.append("conflicting canonical identity")
                    result.fixture_units[key] = units
            if problems:
                invalid += 1
                if len(result.errors) < 50:
                    result.errors.append(f"{path.name}:{number}: {'; '.join(problems)}")
        if count == 0 and schema in ("real_acct", "building_res"):
            result.errors.append(f"{path.name}: required source contains no records")
    result.files.append(
        {
            "path": str(path),
            "schema": schema,
            "records": count,
            "invalid": invalid,
            "observed_source_years": sorted(years),
        }
    )


def validate_harris_sources(
    config: ETLConfig, sources: list[DataSource], year: int, operation: ImportOperation
) -> HarrisSourceValidation:
    result = HarrisSourceValidation()
    manager = ExtractManager(config)
    selected: dict[str, list[Path]] = {schema: [] for schema in _LAYOUTS}
    gis_paths = []
    for source in sources:
        root = manager.get_extract_path(source)
        archive = config.download_dir / source.filename
        acquired = operation.evidence.get("acquired_sources", {}).get(source.filename, {})
        previous_download = operation.evidence.get("reused_from", {}).get(
            str(config.download_dir.parent.parent)
        )
        if not acquired and previous_download:
            previous = ImportOperation.objects.get(pk=previous_download)
            acquired = previous.evidence.get("acquired_sources", {}).get(source.filename, {})
        if acquired:
            operation.evidence.setdefault("acquired_sources", {})[source.filename] = acquired
            if acquired.get("source_year") is not None and acquired["source_year"] != year:
                result.errors.append(
                    f"{source.name}: acquired source year {acquired['source_year']} differs from requested {year}"
                )
        record_sources(
            operation,
            [archive],
            source_id=source.source_id.value if source.source_id else source.name,
            requested_year=year,
            source_year=acquired.get("source_year"),
            source_url=acquired.get("source_url"),
        )
        if archive.exists():
            try:
                with zipfile.ZipFile(archive) as contents:
                    if contents.testzip() is not None:
                        result.errors.append(f"{archive.name}: corrupt archive content")
            except (OSError, zipfile.BadZipFile, NotImplementedError) as exc:
                result.errors.append(f"{archive.name}: archive integrity unavailable: {exc}")
        if source.source_type == DataSourceType.GIS_DATA:
            layer = select_preferred_gis_shapefile(
                [root, config.download_dir / Path(source.filename).stem]
            )
            if layer is None:
                result.errors.append(f"{source.name}: no requested GIS layer")
            else:
                gis_paths.append(layer)
                paths = (
                    list(layer.rglob("*"))
                    if layer.is_dir()
                    else list(layer.parent.glob(f"{layer.stem}.*"))
                )
                record_sources(
                    operation,
                    paths,
                    source_id=source.source_id.value if source.source_id else source.name,
                    requested_year=year,
                    source_year=None,
                )
                _check_archive_inputs(archive, paths, operation, result, relative_to=layer.parent)
            continue
        files = sorted(root.rglob("*.txt"))
        missing = DEFAULT_HCAD_SOURCE_CATALOG.missing_required_files(
            source, [str(path) for path in files]
        )
        if missing:
            result.errors.append(f"{source.name}: required files missing: {', '.join(missing)}")
        detailed = any(path.stem.lower().startswith("extra_features_detail") for path in files)
        for path in files:
            stem = path.stem.lower()
            schema = "extra_features" if stem.startswith("extra_features") else stem
            if schema not in selected or (detailed and stem == "extra_features"):
                continue
            selected[schema].append(path)
            record_sources(
                operation,
                [path],
                source_id=source.source_id.value if source.source_id else source.name,
                requested_year=year,
                source_year=None,
            )
            _inspect_layout(path, schema, year, result)
            _check_archive_inputs(archive, [path], operation, result)
    if result.errors:
        operation.evidence["validation"] = result.evidence()
        return result
    fixtures = FixturesAggregator()
    for path in selected["fixtures"]:
        fixtures.load_fixtures_file(path)
    if not selected["real_acct"]:
        result.account_map = dict(
            PropertyRecord.objects.filter(is_residential=True).values_list("account_number", "pk")
        )
    seen: dict[str, dict[tuple, bytes]] = {schema: {} for schema in selected}
    for schema in ("real_acct", "building_res", "extra_features"):
        counts = {"loaded": 0, "invalid": 0, "skipped": 0}
        for path in selected[schema]:
            rows = (
                iter_property_rows(path)
                if schema == "real_acct"
                else (
                    iter_building_rows(path, result.account_map, fixtures)
                    if schema == "building_res"
                    else iter_extra_feature_rows(path, result.account_map)
                )
            )
            for row in rows:
                if row.skip:
                    counts["skipped"] += 1
                elif row.invalid:
                    counts["invalid"] += 1
                else:
                    record = row.as_dict()
                    fields = (
                        ("account_number",)
                        if schema == "real_acct"
                        else (
                            ("account_number", "building_number")
                            if schema == "building_res"
                            else ("account_number", "feature_code", "feature_number")
                        )
                    )
                    identity = tuple(record[field] for field in fields)
                    content = hashlib.sha256(repr(row.values).encode()).digest()
                    if identity in seen[schema]:
                        if seen[schema][identity] != content:
                            result.errors.append(
                                f"{path.name}: conflicting canonical {schema} identity {identity}"
                            )
                        counts["skipped"] += 1
                        continue
                    seen[schema][identity] = content
                    counts["loaded"] += 1
                    if schema == "real_acct":
                        result.account_map[str(record["account_number"])] = counts["loaded"]
        result.translated[schema] = counts
    for path in gis_paths:
        try:
            inspection = inspect_gis_parcels(str(path), expected_source_year=year)
            coordinates = inspection.coordinates
            result.translated["gis"] = {
                "loaded": len(coordinates),
                "invalid": inspection.invalid,
                "skipped": inspection.skipped,
            }
            result.files.append(
                {
                    "path": str(path),
                    "schema": "gis",
                    "records": len(coordinates) + inspection.invalid + inspection.skipped,
                    "invalid": inspection.invalid,
                    "observed_source_years": list(inspection.source_years),
                }
            )
            if not coordinates:
                result.errors.append(f"{path.name}: requested GIS has no usable coordinates")
        except Exception as exc:
            result.errors.append(f"{path.name}: unusable requested GIS: {exc}")
    operation.evidence["validation"] = result.evidence()
    return result
