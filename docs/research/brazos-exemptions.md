# Brazos property-tax exemption data source

Research for [wayfinder ticket #10](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/10) on the [Brazos County tax-analysis parity map](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/3).

## Question

Where does Brazos County publish/export per-account jurisdiction exemption data — the equivalent of what Harris sources via `import_jur_exemptions` into `PropertyJurisdictionExemption`?

## Finding: present in the certified export, in two files already downloaded — one already partially parsed

Unlike the situs-address, value, and tax-rate questions (#4, #5, #8), exemption data is **not** a dead end for the bulk export. It's split across two files, both already sitting in `var/bcad_extracted/2025/`:

- `APPRAISAL_INFO.TXT` — per-property T/F exemption-type flags. Already the source file for `pacs.py`'s `INFO_LAYOUT`, but the flags themselves aren't decoded yet.
- `APPRAISAL_ENTITY_INFO.TXT` — per-property-per-taxing-entity exemption **dollar amounts** by type, plus the freeze-exemption type code. Not parsed at all today (previously examined only for tax-rate fields in the #8 research and correctly found to lack one — that pass didn't dig into the exemption columns, which is what this ticket adds).

### The missing piece: an official field-layout spec exists and resolves both

BCAD's `certified-data-downloads` page (`https://brazoscad.org/certified-data-downloads/`) publishes, alongside the yearly export ZIPs, a **"File Layout Reference"** PDF: `https://brazoscad.org/wp-content/uploads/2021/07/Appraisal-Export-Layout-8.0.25-2.pdf` ("Appraisal Transfer File Layout", True Automation / PACS, version date 03/24/2021). This is a byte-offset field dictionary for **all 17** `APPRAISAL_*.TXT` files, not just the 5 already reverse-engineered by this project. It is the primary source for everything below; every offset was then independently verified against the real 2025 export bytes (see next sections).

### File #2 — `APPRAISAL_INFO.TXT`: per-property exemption flags

Per the spec, this file (already `pacs.py`'s `INFO_LAYOUT` source, 9,247 chars/line) carries a block of `char(1)` `'T'`/`'F'` flags starting at byte offset 2609 (1-indexed):

| Field | Offset | Meaning |
|---|---|---|
| `ha_exempt` | 2609 | Homestead |
| `ov65_exempt` | 2610 | Over-65 |
| `ov65s_exempt` | ~~2660~~ **2661** [CORRECTED — see Verification (issue #11)] | Over-65 surviving spouse |
| `dp_exempt` | ~~2661~~ **2662** [CORRECTED — see Verification (issue #11)] | Disabled person |
| `dv1_exempt`…`dv4s_exempt` | ~~2662–2670~~ **2663–2670** [CORRECTED — see Verification (issue #11)] | Disabled veteran, 8 flags = 4 rating tiers (10–30% / 30–50% / 50–70% / 70–100%) × self/surviving-spouse |
| `ex_exempt` | 2671 | Total (100%) exemption |
| `ab_exempt`, `en_exempt`, `fr_exempt`, `ht_exempt`, `pro_exempt`, `pc_exempt`, `so_exempt`, `ex366_exempt`, `ch_exempt` | 2723–2731 | Abatement / energy / freeport / historical / prorated / pollution-control / solar / <$500-minimum / charitable — **offset-to-name mapping unverified, see Verification (issue #11)** |

Verified against the real file: sampled the first 149,225 lines of `/home/specter/dev/TaxProtest-Django/var/bcad_extracted/2025/2025-07-23_002022_APPRAISAL_INFO.TXT` at these byte offsets — all four spot-checked fields (`ha_exempt`, `ov65_exempt`, `dp_exempt`, `ex_exempt`) return only `'T'`/`'F'`, with plausible population rates (23.4% homestead, 9.2% over-65, 0.01% disabled-person, 0.006% total-exemption). No garbage/off-by-one noise, i.e. the offsets line up.

> **Correction (issue #11):** the "0.01% disabled-person" rate quoted above was actually measured at offset 2661, which the deeper verification pass proved is `ov65s_exempt` (over-65 surviving spouse, correctly rare at 0.01%), not `dp_exempt`. The real `dp_exempt` is one byte over at offset 2662, with a more demographically plausible population rate of 0.30% (442/149,225). See the Verification section for the full evidence trail.

These flags tell you *which* exemptions apply to a property, but carry no dollar amount — the amount is entity-specific (a $140,000 homestead exemption from an ISD is a different number than a county's), which is why it lives in File #3 instead.

### File #3 — `APPRAISAL_ENTITY_INFO.TXT`: per-entity exemption amounts (the real analog of `PropertyJurisdictionExemption`)

This is the 2.08 GB file at `/home/specter/dev/TaxProtest-Django/var/bcad_extracted/2025/2025-07-23_002022_APPRAISAL_ENTITY_INFO.TXT` (2,750 chars/line, no header, no delimiter — fixed-width like every other PACS file here). Per the spec it's short-named `PROP_ENT.TXT`, keyed on `prop_id` + `prop_val_yr` + `entity_id`, i.e. one row per property × taxing entity × year — structurally exactly what `PropertyJurisdictionExemption` wants, just wide (one column per exemption type) instead of long (one row per exemption type).

Relevant fields per the spec (1-indexed byte offsets):

| Field | Offset | Meaning |
|---|---|---|
| `prop_id` | 1–12 | Property ID |
| `prop_val_yr` | 13–17 | Tax year |
| `entity_id` | 42–53 | Internal entity ID |
| `entity_cd` | 54–63 | Entity code (joins to `APPRAISAL_ENTITY.TXT`'s `entity_code`, already used for tax rates in #8) |
| `entity_name` | 64–113 | Entity name |
| `assessed_val` | 149–163 | Assessed value for this entity |
| `taxable_val` | 164–178 | Taxable value for this entity (post-exemption) |
| `hs_amt`, `ov65_amt`, `dp_amt`, `dv_amt`, `ex_amt`, `ch_amt`, `ab_amt`, `en_amt`, `fr_amt`, `ht_amt`, `pro_amt`, `pc_amt`, `so_amt`, `ex366_amt`, `lve_amt`, `eco_amt`, `chodo_amt`, `lh_amt`, `dvhs_amt`, `dvhss_amt`, `clt_amt`, `dvch_amt`, `dvchs_amt`, `masss_amt`, `frss_amt`, `abmno_amt`, `dstr_amt`, `dstrs_amt` | 299–2614 (scattered) | Dollar amount of each exemption type granted **by this entity** |
| `freeze_exempt_type_cd` | 1615–1619 | Which exemption triggered this entity's tax ceiling/freeze (school over-65/disabled freeze), e.g. `'OV65'`, `'DP'` |
| `freeze_transfer_exempt_type_cd` | 1620–1624 | Same, for a transferred freeze |

Byte-level verification, done against a real, exempt row (`prop_id 000000010013`, `entity_cd G1` = Brazos County, tax year 2025):

```
prop_id                        [1:12]   = '000000010013'
prop_val_yr                    [13:17]  = '02025'
entity_id                      [42:53]  = '000000237993'
entity_cd                      [54:63]  = 'G1        '
entity_name                    [64:113] = 'BRAZOS COUNTY'
assessed_val                   [149:163] = '000000000242613'  -> $242,613
taxable_val                    [164:178] = '000000000167613'  -> $167,613
hs_amt                         [299:313] = '000000000000000'  -> $0
ov65_amt                       [314:328] = '000000000075000'  -> $75,000
dp_amt                         [329:343] = '000000000000000'  -> $0
dv_amt                         [344:358] = '000000000000000'  -> $0
freeze_exempt_type_cd          [1615:1619] = 'OV65 '
```

`assessed_val - taxable_val` = `$242,613 - $167,613` = `$75,000`, exactly equal to `ov65_amt`. And BCAD's own published local-exemption reference (see below) states Brazos County's optional over-65 homestead exemption is **exactly $75,000** — an exact match against an independent source, not just internal arithmetic consistency.

Note on decimal scaling: unlike the money fields in the 5 already-decoded files (`_money` in `pacs.py`, 2 implied decimal places), these `*_amt`/`assessed_val`/`taxable_val` fields appear to be **plain, unscaled dollar integers** — `'000000000075000'` reads as `$75,000`, not `$750.00`. This is inferred from the exact match to the published $75,000 rate, not from the spec (which just says `numeric(15)` with no scaling note). Confirm this convention against several more rows — ideally a non-round-number one — before writing a parser.

Also sampled the `freeze_exempt_type_cd` field independently across 755,602 rows (before finding the spec) and found only a small, plausible vocabulary: `OV65` (31,416), `OV65 OV65` (2,955 — likely joint owners both over 65), `DP` (1,103), `OV65S` (51), `DP   DP` (40), `DPS` (7). Consistent with the spec's documented use as an exemption-type code.

### Version drift, flagged but not resolved

The spec PDF's own revision history ends in 2006 and its File #3 field table runs to byte 2614 (`dstrs_allocation_factor`), but the real 2025 export's lines are 2,750 chars — about 136 bytes longer than the documented layout accounts for. All offsets checked above lined up exactly regardless, but this means the spec may be missing fields added after 2021 (True Automation's disaster-exemption fields, marked in red as "recently added," suggest the format is still evolving) — do not assume the tail of the record is fully covered by this document without checking against additional real rows.

### BCAD website corroboration

- **`https://brazoscad.org/certified-data-downloads/`** — hosts the certified export ZIPs *and* the file-layout PDF above. Also linked from here: **`https://brazoscad.org/wp-content/uploads/2025/12/Exemption-amounts-2025.pdf`** ("Local Exemptions 2025") — a *reference table*, not per-account data: statutory/local-option exemption dollar amounts by taxing unit (Residential Homestead / Over-65 Homestead / Disabled Persons, split Required vs. Optional) plus a disabled-veteran disability-rating → exemption-amount table. This is what confirmed the $75,000 Brazos County figure above. Useful as a cross-check / fallback default table, not as an ingest source (no account numbers anywhere in it).
- **`https://brazoscad.org/tax-information/gis/`** — GIS parcel shapefile, already the primary source for values/situs/building characteristics per #4–#7; not re-checked for exemptions here since the certified export already answers the question more precisely (per-account, per-entity, per-exemption-type dollar amounts — richer than anything a shapefile attribute table would carry).

### Notable side-finding

The `Appraisal-Export-Layout-8.0.25-2.pdf` found here is a general field dictionary for **all 17** `APPRAISAL_*.TXT` files (headers, property, entity association, entity totals, abstract/subdivision, state codes, improvements, land, agent, ARB, lawsuits, entity lookup, UDI conversion, country codes, arbitration, mobile homes, tax deferral) — not just the two used for this ticket. Worth keeping in mind for any future Brazos wayfinder ticket that needs to decode one of the still-unexamined files; it removes the guesswork the 5-already-decoded-files methodology in `pacs.py`'s docstring otherwise required.

## Recommendation

Data model mapping to `PropertyJurisdictionExemption`:

- `account_number` ← `prop_id`
- `tax_year` ← `prop_val_yr`
- `tax_unit_code` ← `entity_cd` (File #3) — already the join key used for `TaxUnitRate` via the adopted-rates scrape (#8)
- `exemption_code` / `exemption_amount` ← unpivot File #3's wide `*_amt` columns into one row per non-zero column (`hs_amt` → code `HS`, `ov65_amt` → code `OV65`, `dp_amt` → code `DP`, `dv_amt` → code `DV`, etc.)
- `assessed_value` / `taxable_value` ← `assessed_val` / `taxable_val` (File #3, already per-entity, no derivation needed)
- The File #2 T/F flags (`ha_exempt`, `ov65_exempt`, …) are redundant with File #3's non-zero `*_amt` columns for "does this exemption apply" — File #3 alone is likely sufficient for ingestion; File #2's flags are a useful cheap validation cross-check (flag `T` should imply a non-zero corresponding `*_amt` row exists in File #3) rather than a separate ingest source.

Before writing an ingest command: (1) confirm the no-decimal-scaling interpretation of File #3's money fields against several more real rows, ideally non-round-number ones; (2) re-derive the field offsets from real bytes rather than trusting the spec verbatim past ~byte 2614, given the documented drift; (3) decide the exemption-code vocabulary to standardize on (the spec's field-name suffixes — `HS`, `OV65`, `OV65S`, `DP`, `DPS`, `DV1`–`DV4` + `S` variants, `EX`, `AB`, `EN`, `FR`, `HT`, `PRO`, `PC`, `SO`, `EX366`, `CH`, `LVE`, `ECO`, `CHODO`, `LH`, `DVHS`, `DVHSS`, `CLT`, `DVCH`, `DVCHS`, `MASSS`, `FRSS`, `ABMNO`, `DSTR`, `DSTRS` — is a superset of Harris's exemption codes and should probably be used as-is for `exemption_code`). This ticket is locate-only, per the usual convention (#4–#8) — no ingestion command was built here.

## Verification (issue #11)

The prior pass above checked exactly one field (`ov65_amt`) on one row. This pass samples several real properties across different exemption shapes and cross-references every claimed offset against internally-consistent real data (the `APPRAISAL_INFO.TXT` flags, `APPRAISAL_ENTITY_INFO.TXT`'s dollar amounts, and `freeze_exempt_type_cd`, which are three independently-populated fields that must agree if the offsets are right). It also attempted a live cross-check against BCAD's own public property-search site.

### Methodology

Wrote small ad-hoc Python scripts (not committed — one-off scans, discarded after use) against the real, already-downloaded 2025 export at `var/bcad_extracted/2025/`:

- `2025-07-23_002022_APPRAISAL_INFO.TXT` — 149,225 lines, 9,247 content chars/line + CRLF (confirmed by measuring raw byte length of the first two lines: 9,249 bytes each, minus 2 for `\r\n`).
- `2025-07-23_002022_APPRAISAL_ENTITY_INFO.TXT` — 755,602 lines, 2,750 content chars/line + CRLF (measured the same way: 2,752 bytes/line).

Both files were read in **binary mode** and sliced by raw byte offset (not text-mode `open()`, which would risk universal-newline/encoding surprises) — `chr(byte)` for single-char flags, `.decode('latin-1')` for multi-byte fields, matching the ASCII-only content actually observed.

Steps: (1) frequency-scanned every byte position in the `APPRAISAL_INFO.TXT` flag region (2600–2745) across all 149,225 lines to see which offsets ever contain `'T'` vs. which are constant space (a cheap, file-wide way to distinguish real flag bytes from padding, independent of the spec); (2) picked `prop_id`s showing different flag shapes (homestead-only, over-65+homestead, disabled-person, disabled-veteran tiers, total-exemption) and dumped their raw byte windows; (3) looked up the same `prop_id`s in `APPRAISAL_ENTITY_INFO.TXT` and checked whether `assessed_val − taxable_val` equals the sum of that entity's `*_amt` fields, and whether `freeze_exempt_type_cd` (a plain-text code like `'OV65'`, `'DP'`, `'OV65S'`, `'DPS'`) agrees with which `*_amt` field is non-zero — this is the cross-check that caught the offset error below, since a flag claiming "disabled person" next to entity rows that actually say `freeze_exempt_type_cd = 'OV65S'` is a direct contradiction, not a matter of interpretation.

### Sample accounts checked

| `prop_id` | `APPRAISAL_INFO.TXT` flags observed (corrected offsets) | `APPRAISAL_ENTITY_INFO.TXT` cross-check | Verdict |
|---|---|---|---|
| `000000010013` | ha=T, ov65=T | G1 (Brazos County): `ov65_amt`=$75,000, `assessed−taxable`=$75,000, `freeze='OV65 '`. S1 (Bryan ISD): `hs_amt`=$100,000, `ov65_amt`=$2,946 (non-round), sum=$102,946=`assessed−taxable` exactly. | Match — also the doc's original spot-check row |
| `000000010055` | ha=T only | S1: `hs_amt`=$100,000, `assessed−taxable`=$100,000, no `ov65_amt`/`dp_amt`. G1: no exemption at all (county grants no local homestead here). | Match |
| `000000010179`, `000000010209`, `000000010335` | ha=T, ov65=T, one DV-tier flag each (2663, 2670, 2665 respectively) | G1: `ov65_amt`=$75,000, `freeze='OV65 '`, plus `dv_amt`=$12,000 on every entity row (CAD/F2/G1/S1/ZRFND alike — a DV exemption applies district-wide, not per-taxing-unit like HS/OV65). | Match |
| `000000010097` | ha=T, ov65=T, DV-tier flag at 2669 | `taxable_val` = **$0** on every entity (CAD/F4/G1/S1/ZRFND), but `dv_amt`=$0 too — the full value is exempted through some other, unsampled column, not the `dv_amt` offset this doc tracks. | Partial — see "dv_amt" caveat below |
| `000000014115`, `000000016359`, `000000019107`, `000000027133`, `000000027474` | ha=T, flag at (corrected) 2661=T, all else in the block F | Every one of these: `freeze_exempt_type_cd = 'OV65S'` and non-zero `ov65_amt` (not `dp_amt`) on G1/S1. `000000027133` G1: `ov65_amt`=$26,935 (non-round) + `dv_amt`=$12,000 = $38,935 = `assessed−taxable` exactly. | Confirms offset 2661 is `ov65s_exempt`, **not** `dp_exempt` as originally documented |
| `000000010791`, `000000010803`, `000000011073`, `000000012421`, `000000012599` | ha=T, flag at (corrected) 2662=T | `freeze_exempt_type_cd = 'DP'` (or `'DPS'` for 010791) and `dp_amt`=$10,000 on S1 (Bryan ISD) for every one of these. | Confirms offset 2662 is the real `dp_exempt` |
| `000000367255`, `000000367313`, `000000367516` | ex=T (2671) only, no other flags in the block | `taxable_val` = $0 on every entity for `000000367255` (checked: CAD/City/G1/ISD/ZRFND all zero), consistent with a total exemption. | Match |

### Offset corrections (APPRAISAL_INFO.TXT)

The flag block from `ov65s_exempt` through the disabled-veteran tiers is shifted **one byte later** than originally documented; `ha_exempt` (2609), `ov65_exempt` (2610), and `ex_exempt` (2671) are unaffected and confirmed correct:

| Field | Originally documented | Corrected | Evidence |
|---|---|---|---|
| `ov65s_exempt` | 2660 | **2661** | Offset 2660 is a space character on all 149,225 rows, file-wide — it is never populated, so it cannot be a real flag. Offset 2661 correlates 100% (5/5 sampled rows, plus the file-wide 0.013% rate) with `freeze_exempt_type_cd='OV65S'` and non-zero `ov65_amt` in the entity file. |
| `dp_exempt` | 2661 | **2662** | Offset 2662 correlates 100% (5/5 sampled rows, plus the file-wide 0.30% rate) with `freeze_exempt_type_cd='DP'`/`'DPS'` and non-zero `dp_amt` in the entity file. |
| `dv1_exempt`…`dv4s_exempt` | 2662–2670 (9 slots) | **2663–2670 (8 slots)** | Follows directly from the above: once `dp_exempt` is correctly placed at 2662, the disabled-veteran block is the remaining 8 bytes through 2670, which also resolves an odd asymmetry in the original 9-slot range — 8 slots divide cleanly into 4 rating tiers × self/surviving-spouse. Exact per-tier semantic identity (which of the 8 bytes is "10–30% self" vs. "70–100% surviving spouse" etc.) was **not** determined this pass; see caveat below. |

`ex_exempt` (2671) is confirmed correct: all 3 `ex=T` sample rows show `taxable_val=$0` across every taxing entity, consistent with a total exemption.

**`dv_amt` (APPRAISAL_ENTITY_INFO.TXT, offset 344–358) caveat:** this single offset does **not** represent a combined/total disabled-veteran dollar amount. Three sampled properties with a DV-tier flag set show `dv_amt=$12,000` (consistent with the Texas statutory 70–99%-disabled amount), but `000000010097` — which also has a DV-tier flag set (at byte 2669, a *different* position than the other three) — shows `dv_amt=$0` on every entity row while its `taxable_val` is fully zeroed out. The most likely explanation is that PACS keeps **separate** `dv1_amt`/`dv1s_amt`/…/`dv4_amt`/`dv4s_amt` columns (mirroring the 8 DV flags), and this research only located one of them; a 100%-disabled/unemployable veteran's exemption is probably realized through a different column (or through `ex_amt`) rather than through this offset. Do not treat the single `dv_amt` offset documented above as capturing all disabled-veteran exemption dollars — it needs the same kind of per-flag isolation done for `ov65s_exempt`/`dp_exempt` before it's ingest-ready.

### Second flag block (`ab_exempt`…`ch_exempt`, 2723–2731): still unverified

A file-wide frequency scan of offsets 2722–2731 confirms this whole span really is a run of independent `'T'`/`'F'` bytes (not padding) with plausible-looking, individually-varying T-counts (2, 0, 49, 0, 0, 46, 44, 15,203, 0 respectively for offsets 2722–2731). However:

- There is a legitimate, always-`'F'`-but-structurally-real flag byte at **offset 2722**, immediately before the documented `ab_exempt` at 2723, that the original doc's table doesn't account for at all.
- Unlike the `ov65s`/`dp`/`dv`/`ex` block, there is no independent field in `APPRAISAL_ENTITY_INFO.TXT` sampled in this research (a per-type `ab_amt`, `en_amt`, etc.) to cross-reference these against, the way `freeze_exempt_type_cd` and `ov65_amt`/`dp_amt` caught the error in the first block. The offset-to-specific-name mapping for `ab_exempt`/`en_exempt`/`fr_exempt`/`ht_exempt`/`pro_exempt`/`pc_exempt`/`so_exempt`/`ex366_exempt`/`ch_exempt` is therefore **not confirmed** — it may be correct as documented, or may have the same kind of one-byte drift found in the first block (possibly starting at 2722 instead of 2723). Do not rely on these 9 field names without further verification tying each byte to a real entity-file dollar amount or a live BCAD record known to carry that specific exemption type.
- The unusually high T-count at whichever offset maps to `ex366_exempt` (15,203/149,225 ≈ 10.2%) initially looked implausible for a "<$500 minimum value" exemption, but on reflection is plausibly explained by a large county with many small mineral-interest/personal-property/utility-easement parcels — this is a plausibility judgment, not a confirmed match, and is called out here rather than silently accepted.

### No-decimal-scaling assumption: confirmed

The original doc verified this against exactly one round-number row (`ov65_amt=$75,000`). This pass adds two more, both **non-round numbers**, which is a much stronger check because a wrong scaling factor would not coincidentally still reconcile the arithmetic:

- `000000010013`, entity S1 (Bryan ISD): `hs_amt`=$100,000 + `ov65_amt`=**$2,946** = $102,946, exactly equal to `assessed_val − taxable_val` ($242,613 − $139,667). If `ov65_amt` carried 2 implied decimal places (like `_money` in `pacs.py`), the raw digits would mean $29.46, not $2,946 — the arithmetic would be off by 100x and would not reconcile. It does reconcile, exactly, as plain dollars.
- `000000027133`, entity G1 (Brazos County): `ov65_amt`=**$26,935** + `dv_amt`=$12,000 = $38,935, exactly equal to `assessed_val − taxable_val` ($38,935 − $0). Same conclusion.

Both confirm: `APPRAISAL_ENTITY_INFO.TXT`'s `*_amt`/`assessed_val`/`taxable_val` fields are plain, unscaled dollar integers, unlike the 2-implied-decimal money fields elsewhere in this export family. The `_money` helper in `pacs.py` must **not** be reused for these fields if/when this file is wired into `INGEST_SPECS` — a plain-integer cast (or a `_money`-like helper with a scale of 1, not 100) is needed instead.

### Live BCAD cross-check: attempted, not achievable with available tooling

Per the ticket, tried to find and use BCAD's own public per-account search tool as an independent, external cross-check (rather than only cross-referencing the export's internal fields against each other):

- `brazoscad.org`'s homepage has a "PROPERTY SEARCH" button linking to **`https://esearch.brazoscad.org/`** — a real, live, public BCAD property-search portal (not a `propaccess.trueautomation.com` URL as guessed in the ticket; BCAD appears to self-host its own eSearch front-end rather than using True Automation's hosted PropAccess product, even though the certified export itself is True Automation/PACS format).
- The search UI (By Owner / By Address / By ID / ARB Search / Advanced tabs) is confirmed live and reachable, showing a real disclaimer about "2026 PRELIMINARY VALUES."
- Attempted direct deep-links to individual property records for sampled `prop_id`s (e.g. `10013`) using several plausible query-string patterns against `Property.aspx` (`?prop_id=10013`, `?PropertyID=10013`, `?p=10013&year=2025`) — **all three returned the site's generic `ERROR 404: Page Not Found` page**, not a valid record and not a "property not found" message specific to that ID (the same generic error appeared for every pattern tried, which is itself the tell).
- Conclusion: this looks like a standard ASP.NET WebForms search app that generates result links via server-side postback with a session-scoped token, rather than supporting bare `?prop_id=` deep-linking. `WebFetch` (single-shot GET + HTML→markdown, no JavaScript execution, no form POST, no session/cookie continuity across calls) cannot drive that flow. This is a genuine tooling limitation, not a "no exemptions matched" finding — no live BCAD data was actually compared against the byte-offset decoding in this pass.
- In place of the live cross-check, this pass relied on the internal-consistency method described above (three independently-populated fields — `APPRAISAL_INFO.TXT` flags, `APPRAISAL_ENTITY_INFO.TXT` dollar amounts, and `freeze_exempt_type_cd` — that must agree, and did, across 15 real accounts, catching a real one-byte offset error in the process). That is meaningfully stronger than the original single-field, single-row check, but it is still not the same as an external ground-truth comparison against BCAD's own published per-account record.

### Summary of what changed vs. the original finding

- **Confirmed as originally documented:** `ha_exempt` (2609), `ov65_exempt` (2610), `ex_exempt` (2671); `APPRAISAL_ENTITY_INFO.TXT`'s `prop_id`, `prop_val_yr`, `entity_id`, `entity_cd`, `entity_name`, `assessed_val`, `taxable_val`, `hs_amt`, `ov65_amt`, `dp_amt`, `freeze_exempt_type_cd`; the no-decimal-scaling assumption for File #3's money fields (now checked against 3 rows including 2 non-round numbers, not just 1 round one).
- **Corrected:** `ov65s_exempt` 2660→2661, `dp_exempt` 2661→2662, DV-tier flags 2662–2670→2663–2670 (8, not 9, slots).
- **Refined (not wrong, but narrower than implied):** `dv_amt` (344–358) is one of several likely per-tier disabled-veteran amount columns, not a combined total — needs further isolation before ingest.
- **Still unconfirmed:** the `ab_exempt`…`ch_exempt` block's exact offset-to-name mapping (2722 vs. 2723 start, and no independent field to cross-check the 9 names against).
- **Not achievable this pass:** live cross-reference against BCAD's own public property-search tool (`esearch.brazoscad.org`) — the site was located and is real/public, but doesn't support the kind of direct per-account deep-linking that the available fetch tooling can drive.
