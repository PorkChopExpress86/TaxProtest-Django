import csv
import os
from typing import Iterable, Dict, Optional, List

from django.db import transaction

from .models import PropertyRecord


def sniff_delimiter(sample: str) -> str:
    """Try to detect delimiter among comma, tab, and pipe."""
    candidates = ["\t", "|", ","]
    counts = {d: sample.count(d) for d in candidates}
    # choose the delimiter with the max count (prefer tab/pipe over comma if tied)
    return max(counts, key=lambda d: (counts[d], 1 if d in ("\t", "|") else 0))


def open_reader(filepath: str) -> csv.DictReader:
    """Open a large text file and return a DictReader with detected delimiter.

    Handles UTF-8 with fallback to latin-1 if needed.
    """
    # Read a small sample to sniff
    with open(filepath, "rb") as f:
        sample_bytes = f.read(4096)
    try:
        sample = sample_bytes.decode("utf-8", errors="ignore")
        encoding = "utf-8"
    except Exception:
        sample = sample_bytes.decode("latin-1", errors="ignore")
        encoding = "latin-1"

    delimiter = sniff_delimiter(sample)

    # We need to re-open as text for DictReader
    f = open(filepath, "r", encoding=encoding, errors="ignore", newline="")
    # CSV may or may not have header; HCAD files generally include headers.
    return csv.DictReader(f, delimiter=delimiter)


def parse_currency(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    # remove $ and commas
    for ch in ["$", ","]:
        v = v.replace(ch, "")
    try:
        return float(v)
    except Exception:
        return None


def iter_property_rows(reader: csv.DictReader) -> Iterable[Dict]:
    """Yield normalized property rows from a Real Account file.

    This expects columns typically present in real_acct.txt such as:
      - SITE_ADDR_NUM, SITE_ADDR_STREET (or SITE_ADDR), ZIP
      - OWNER_NAME (or similar)
      - APPR_BLDG_VAL, APPR_LAND_VAL, APPR_EXFEAT_VAL, TOT_APPR_VAL, MKT_VAL
    We fallback to reasonable alternatives; unknown columns are ignored.
    """
    # Normalize field names to lowercase for matching
    lower_fieldnames = [fn.lower() for fn in (reader.fieldnames or [])]
    fieldmap = {fn.lower(): fn for fn in (reader.fieldnames or [])}

    def get(row: Dict, *names: str) -> str:
        for n in names:
            key = fieldmap.get(n.lower())
            if key and key in row:
                return (row.get(key) or "").strip()
        return ""

    for row in reader:
        # Address components (HCAD specific: str_num/str/str_sfx; or site_addr_1 as full)
        addr_num = get(row, "str_num", "site_addr_num", "situs_addr_num", "address_number")
        addr_num_sfx = get(row, "str_num_sfx")
        street_name = get(row, "str", "site_addr_street", "situs_street", "street_name")
        street_pfx = get(row, "str_pfx")
        street_sfx = get(row, "str_sfx")
        street_sfx_dir = get(row, "str_sfx_dir")
        site_addr_1 = get(row, "site_addr_1", "site_addr")
        site_city = get(row, "site_addr_2", "situs_city", "city")
        raw_zip = get(row, "site_addr_3", "zip", "zip_code", "zipcode")
        zipcode = (raw_zip[:5] if raw_zip and len(raw_zip) >= 5 else raw_zip)

        # Market or total value
        value = (
            parse_currency(get(row, "tot_appr_val"))
            or parse_currency(get(row, "mkt_val"))
            or parse_currency(get(row, "appr_bldg_val"))
        )
        assessed_value = parse_currency(get(row, "assessed_val", "assessed_val", "tot_appr_val"))
        building_area = parse_currency(get(row, "bld_ar", "bldg_ar", "bld_area"))
        land_area = parse_currency(get(row, "land_ar", "land_area"))

        account_number = get(row, "acct", "account", "account_number")
        owner_name = get(row, "mailto", "owner_name", "owner")

        # Build address: prefer explicit components; fallback to site_addr_1
        number = "".join([p for p in [addr_num, addr_num_sfx] if p])
        street_parts = [street_pfx, street_name, street_sfx, street_sfx_dir]
        street = " ".join([p for p in street_parts if p])
        built_address = " ".join([p for p in [number, street] if p]).strip()
        address = built_address or site_addr_1

        yield {
            "address": address,
            "city": site_city,
            "zipcode": zipcode,
            "value": value,
            "assessed_value": assessed_value,
            "building_area": building_area,
            "land_area": land_area,
            "account_number": account_number,
            "owner_name": owner_name,
            "street_number": number,
            "street_name": street_name,
        }


def bulk_load_properties(filepath: str, chunk_size: int = 5000, limit: Optional[int] = None) -> int:
    """Load a Real Account file into PropertyRecord table.

    Returns number of rows inserted.
    """
    reader = open_reader(filepath)
    buf: List[PropertyRecord] = []
    total = 0
    with transaction.atomic():
        for idx, data in enumerate(iter_property_rows(reader), start=1):
            # simple validation: require either address or zipcode
            if not (data.get("address") or data.get("zipcode")):
                continue
            buf.append(
                PropertyRecord(
                    address=data.get("address", "")[:255],
                    city=data.get("city", "")[:100],
                    zipcode=data.get("zipcode", "")[:20],
                    value=data.get("value"),
                    assessed_value=data.get("assessed_value"),
                    building_area=data.get("building_area"),
                    land_area=data.get("land_area"),
                    account_number=data.get("account_number", "")[:20],
                    owner_name=data.get("owner_name", "")[:255],
                    street_number=data.get("street_number", "")[:16],
                    street_name=data.get("street_name", "")[:128],
                    source_url="hcad:real_acct",
                )
            )
            if len(buf) >= chunk_size:
                PropertyRecord.objects.bulk_create(buf)
                total += len(buf)
                buf.clear()
            if limit and total >= limit:
                break
        if buf:
            PropertyRecord.objects.bulk_create(buf)
            total += len(buf)
    return total
