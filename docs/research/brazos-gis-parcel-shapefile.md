# Brazos GIS parcel shapefile characterization

Research for [wayfinder ticket #7](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/7) on the [Brazos County tax-analysis parity map](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/3).

## Question

Characterize the Brazos GIS parcel shapefile's schema for lat/long coordinate ingestion, mirroring Harris's `load_gis_data` / `Parcels.zip` pipeline.

## Source

`https://brazoscad.org/wp-content/uploads/2026/05/BrazosCADParcels_20260422.zip` (found via `brazoscad.org/tax-information/gis/` — the filename guessed during ticket charting was close but not exact; the download page is the reliable way to find the current year's link). 27MB zip, standard Esri shapefile bundle (`.shp`/`.shx`/`.dbf`/`.prj`/`.cpg`/`.sbn`/`.sbx` + a `.shp.xml` metadata file). **77,433 features.**

## Coordinate reference system

`EPSG:2277` — NAD83 / Texas Central (US feet). Needs reprojection to WGS84 (`EPSG:4326`) for lat/long, the same as Harris's pipeline already does (see `docs/guides/GIS.md`). Verified: centroid reprojection of 3 sample parcels lands at real Bryan, TX coordinates (~30.68°N, -96.37°W), confirming the transform is correct.

## Join key

`PROP_ID` (also duplicated as `prop_id_1`) — a **plain integer** (e.g. `10002`), NOT the zero-padded 12-char string `brazos_cad.PropertyAccount.prop_id` uses (`"000000010002"`). Needs `int()`/zero-pad conversion when joining. Confirmed present and populated on essentially all 77,433 features.

## Full attribute field list

```
PROP_ID, last_edite, prop_id_1, geo_id, Sale_Date, sl_price, sl_ratio, file_as_na,
addr_line1, addr_line2, addr_line3, addr_city, addr_state, addr_zip,
situs_num, situs_stre, situs_st_1, situs_st_2, situs_unit,
legal_desc, abs_subdv_, Deed_Date, deed_book_, deed_book1, sl_type_cd, hood_cd,
Entities, Exemptions,
market, Land_Val, Imprv_Val, state_cd, ls_table, land_acres, land_sqft,
living_are, class_cd, imprv_unit, yr_built, yr_blt, land_unit_,
ImpCnt, Group_Code, Group_Co_1, Group_Co_2,
Shape_Leng, Shape_Area, geometry
```

## What this answers on the map (see individual ticket docs for full detail)

- **[Ticket #4](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/4) (situs address)** — `situs_num`/`situs_stre`/`situs_st_1`/`situs_st_2`/`situs_unit` = the true physical address, distinct from `addr_line1`/`addr_city`/`addr_state`/`addr_zip` (the mailing address, confirmed to exactly match what's already decoded from `APPRAISAL_INFO.TXT`). 99.65% populated (77,165/77,433). See [docs/research/brazos-situs-address.md](https://github.com/PorkChopExpress86/TaxProtest-Django/blob/research/brazos-situs-address/docs/research/brazos-situs-address.md).
- **[Ticket #5](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/5) (values + state_class)** — `market`, `Land_Val`, `Imprv_Val`, `state_cd` all present as clean typed fields, no fixed-width decoding needed. `Land_Val + Imprv_Val == market` exactly on a 2,000-row sample, **0 mismatches**. `state_cd` distribution looks like real Texas property-class codes (A1 dominant at 48,195 rows — plausibly residential; also C1/F1/A7/E1/A2/B2/D1/A8/E4/etc). See [docs/research/brazos-values.md](https://github.com/PorkChopExpress86/TaxProtest-Django/blob/research/brazos-values/docs/research/brazos-values.md).
- **[Ticket #6](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/6) (building characteristics)** — `living_are` (sqft), `yr_built`/`yr_blt` (age — cross-validates *exactly* against this codebase's own independently-derived `year_built` rollup for a real test property: both give 1980), `class_cd` (quality/construction-type-like code, values like `RV3`/`RF4`/`RV4P` suggest R=residential, V/F=veneer/frame, digit=quality tier, P=partial — not fully confirmed). **No bedroom or bathroom field anywhere in this shapefile** — those come only from `APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT` (see [docs/research/brazos-building-characteristics.md](https://github.com/PorkChopExpress86/TaxProtest-Django/blob/research/brazos-building-characteristics/docs/research/brazos-building-characteristics.md)), already resolved. `ImpCnt` (improvement count) exists but isn't a real substitute for "stories" — still unconfirmed.
- **Ticket #8 (entity tax rates)** — not investigated for overlap in this pass; the shapefile has `Entities`/`Exemptions` list-like text fields per parcel, but no rate percentage was spotted in a quick glance. Resolved separately/externally — see [docs/research/brazos-entity-tax-rates.md](https://github.com/PorkChopExpress86/TaxProtest-Django/blob/research/brazos-entity-tax-rates/docs/research/brazos-entity-tax-rates.md).

## Cross-validation against the test property used throughout this codebase's Brazos work

`prop_id=000000010002` (STASNY FAMILY RANCH LLC):

- Mailing (shapefile `addr_line1`/`addr_city`/`addr_state`/`addr_zip`): blank / "COLLEGE STATION" / "TX" / "77845-8087" — **exactly matches** what's already decoded from `APPRAISAL_INFO.TXT`, confirming that block really is mailing address, as suspected.
- Situs (shapefile `situs_num`/`situs_st_1`/`situs_st_2`): "5000 SILVER HILL RD" — a distinct, plausible rural address for a ranch property, genuinely different from the mailing address.
- Values: `market`=$5,501,797, `Land_Val`=$5,081,302, `Imprv_Val`=$420,495 — consistent with a large ranch property (`land_acres`=573.9).
- `yr_built`=1980 — matches this codebase's own independently-computed `year_built` rollup (from `PropertyImprovementDetail`) for the same property exactly.

## Not yet done

This ticket was characterization only — no ingestion command was designed or built. Whoever picks this up next should mirror Harris's `load_gis_data` pattern (see `data/management/commands/load_gis_data.py` and `docs/guides/GIS.md`), reproject `EPSG:2277` → `EPSG:4326`, and reconcile the `PROP_ID` zero-padding mismatch against `PropertyAccount.prop_id`.

The raw shapefile was downloaded to a session-only temp path during this research (not committed, not on a durable path) — re-download from the source URL above when implementing.
