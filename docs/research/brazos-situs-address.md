# Brazos true situs (property) address

Research for [wayfinder ticket #4](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/4) on the [Brazos County tax-analysis parity map](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/3).

## Question

Where does BCAD's data carry the property's TRUE situs (physical) address, distinct from the owner-mailing-address block already decoded at `APPRAISAL_INFO.TXT` offsets 753:987 — which is what `/brazos/` incorrectly shows today?

## Finding: not in `APPRAISAL_INFO.TXT` at all — it's in the GIS parcel shapefile

### `APPRAISAL_INFO.TXT` confirmed to have no second situs block

Scanned every byte offset across 5,000+ sample rows in `var/bcad_extracted/2025/2025-07-23_002022_APPRAISAL_INFO.TXT` for (a) Brazos-area city name strings and (b) 77xxx/78xxx zip patterns. Found only:

- The known mailing block at offset 873 (city) / 978 (zip).
- An exact duplicate of the identical values at a fixed +3968-byte shift (offset 4741/4846) — confirmed byte-identical to the primary block across 1,978 of 1,978 sampled rows. This is a duplicate of the mailing block, not a distinct situs field.

No other offset — including the unexplored 5000–9247 tail — showed city-name or zip hits above ~2% of rows (noise/coincidence threshold). This file genuinely does not carry situs address anywhere in its decodable text.

### The real situs address lives in the BCAD GIS parcel shapefile

Full schema/CRS/join-key detail is in [docs/research/brazos-gis-parcel-shapefile.md](./brazos-gis-parcel-shapefile.md) (ticket #7's dedicated characterization) — summary here.

Source: `https://brazoscad.org/wp-content/uploads/2026/05/BrazosCADParcels_20260422.zip` (linked from `brazoscad.org/tax-information/gis/`), 77,433 parcel records. Its `.dbf` attribute table has explicit, separate, structured fields for situs vs. mailing address:

**Situs (physical) address fields**: `situs_num`, `situs_stre` (direction), `situs_st_1` (street name), `situs_st_2` (suffix), `situs_unit`. 99.65% populated (77,165/77,433).

**Mailing address fields** (separate, confirmed distinct): `addr_line1` (often a "% AGENT NAME" c/o line, e.g. `'% RODRIGUEZ MARIE P AGENT'`), `addr_city`, `addr_state`, `addr_zip`.

**Join key**: `PROP_ID` (also duplicated as `prop_id_1`) — a plain integer (e.g. `10002`), **not** zero-padded to 12 digits like the `prop_id` used elsewhere in `brazos_cad` (e.g. `'000000010002'`). Format reconciliation needed when joining against `PropertyAccount.prop_id`.

**Cross-validated directly against the test property used throughout this codebase's Brazos work** (`prop_id=000000010002`, STASNY FAMILY RANCH LLC):

- Mailing (shapefile): blank / "COLLEGE STATION" / "TX" / "77845-8087" — **exactly matches** what's already decoded from `APPRAISAL_INFO.TXT`, confirming that block really is mailing address, as suspected.
- Situs (shapefile): "5000 SILVER HILL RD" — a distinct, plausible rural address for a ranch property, genuinely different from the mailing address.

A second example (prop `22567`, Cavitt Janice Jean) shows the same pattern: mailing = "605 N PARKER AVE", situs = "206 W 21ST ST" — different addresses again.

## Recommendation

Whoever implements the fix should use the shapefile's `situs_num`/`situs_stre`/`situs_st_1`/`situs_st_2`/`situs_unit` fields, not `APPRAISAL_INFO.TXT`. `APPRAISAL_INFO`'s address block should stay labeled as owner mailing address (or be dropped from the public-facing "Address" column and replaced by this shapefile data).

## Related findings (out of scope for this ticket, surfaced incidentally)

The same shapefile inspection also found data directly relevant to two sibling tickets:

- **[Ticket #5](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/5) (values/state_class)**: the shapefile has `market`, `Land_Val`, `Imprv_Val` (clean typed numeric fields, e.g. `market=147770, Land_Val=79542, Imprv_Val=68228`) and `state_cd` (e.g. `'A1'`, `'F1'`, `'C1'`) — see [docs/research/brazos-values.md](./brazos-values.md) for the full resolution.
- **[Ticket #6](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/6) (building characteristics)**: `living_are` (sqft, e.g. `1608`), `yr_blt` (e.g. `1991`), `class_cd` (e.g. `'RF1'`, `'RF2P'`) — folded into [docs/research/brazos-building-characteristics.md](./brazos-building-characteristics.md).

See [ticket #7](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/7) (dedicated GIS shapefile characterization) for the authoritative full field list, CRS, and row count.

The raw shapefile was downloaded to a session-only temp path during this research (not committed, not on a durable path) — whoever implements ingestion will need to re-download it from the URL above.
