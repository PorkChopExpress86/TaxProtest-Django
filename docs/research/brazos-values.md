# Brazos assessed/total/market value + state_class

Research for [wayfinder ticket #5](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/5) on the [Brazos County tax-analysis parity map](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/3).

## Question (as originally scoped)

Decode `total_value`, `land_value`, `improvement_value`, `assessed_value`, and `state_class` from `APPRAISAL_INFO.TXT`'s undecoded rollup region, ~byte offset 1745–2200 of the 9,247-char record.

## Finding: the original approach was a dead end — the GIS parcel shapefile answers this directly instead

### Attempt 1: decoding `APPRAISAL_INFO.TXT`'s rollup region — inconclusive, abandoned

The region from offset ~1795 onward is confirmed dense/structured — uniform 15-char zero-padded numeric slots, a flag character, two shorter numeric/comma-formatted fields (likely legal-description/plat references, not values — e.g. `873,1366`, `210,073`), an 8-digit `MMDDYYYY` date (likely a deed date), then more reserved zero fields.

Two candidate offsets were found with **high confidence but the wrong semantics**:
- Offset `1855:1870` (÷10000) matches a land segment's acreage.
- Offset `1870:1885` (÷100) matches a land segment's `land_value`.

But cross-checking against the already-ingested database (`PropertyLand` grouped by `prop_id`) showed these are **per-segment values, not property-wide totals** — a multi-parcel property (common in this rural county) has this offset holding only its *first* segment's value, not the sum. Not usable as "the property's land_value."

Two more candidate offsets (`1840:1855` for `improvement_value`; `1915:1930`/`1945:1960` for `total_value`/`assessed_value`) looked plausible on one anchor row, but the hypothesis `land + improvement = total` held for only 1,117 of 5,000 sampled rows (22%) — not reliable. Cross-checking a specific property (`prop_id=000000010011`) showed the candidate total (`22,379.52`) didn't reconcile with summed real data at all (off by orders of magnitude from a `PropertyImprovementDetail.detail_value` sum of `1,608,845.00`).

`state_class` was not located anywhere near this block — no alphanumeric 2-4 char code resembling Texas state-class codes turned up.

**Conclusion**: `APPRAISAL_INFO.TXT`'s 9,247-char record most likely encodes value data per-segment (per land parcel / per improvement), not as simple property-wide rollups — the record probably isn't "one row = one property with everything summed" the way the original ticket assumed. Confidently decoding this further would need figuring out the record's full repeating-segment structure, which is a much bigger undertaking than the rest of this file's decode.

### Attempt 2: the GIS parcel shapefile — answered directly, no further decoding needed

Full schema/CRS/join-key detail is in [docs/research/brazos-gis-parcel-shapefile.md](https://github.com/PorkChopExpress86/TaxProtest-Django/blob/research/brazos-gis-parcel-shapefile/docs/research/brazos-gis-parcel-shapefile.md) (ticket #7's dedicated characterization). Source: `https://brazoscad.org/wp-content/uploads/2026/05/BrazosCADParcels_20260422.zip`.

The shapefile's `.dbf` attribute table has `market`, `Land_Val`, `Imprv_Val`, and `state_cd` as **clean, already-typed fields** — no byte-offset decoding required at all:

- `market` — total value
- `Land_Val` — land value
- `Imprv_Val` — improvement value
- `state_cd` — property class code (e.g. `A1`, `C1`, `F1`, `A7`, `E1`, `A2`, `B2`, `D1`, `A8`, `E4`)

**Rigorously validated**: `Land_Val + Imprv_Val == market` exactly, checked across a 2,000-row sample — **0 mismatches**. All value fields 100% populated; `state_cd` 99.65% populated. `state_cd` distribution looks like real Texas property-class codes, with `A1` dominant (48,195 of ~77,433 rows — plausibly residential).

Cross-validated against the test property used throughout this codebase's Brazos work (`prop_id=000000010002`, STASNY FAMILY RANCH LLC, a ranch property with `land_acres`=573.9): `market`=$5,501,797, `Land_Val`=$5,081,302, `Imprv_Val`=$420,495 — internally consistent and plausible for the property type.

## Recommendation

Use the GIS parcel shapefile's `market`/`Land_Val`/`Imprv_Val`/`state_cd` fields, not `APPRAISAL_INFO.TXT`'s undecoded region. This also means `is_residential` (currently unpopulated on `PropertyAccount`) becomes derivable once `state_cd` is ingested, the same way Harris derives it from `state_class`.

No `APPRAISAL_INFO.TXT` decoding work remains necessary for this ticket's original goal — the per-segment structure issue documented in Attempt 1 is noted for completeness but doesn't block anything now that the shapefile answers the question directly.
