# Brazos building characteristics (bed/bath/quality/condition/sqft)

Research for [wayfinder ticket #6](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/4) on the [Brazos County tax-analysis parity map](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/3).

## Question

Does BCAD publish structured building characteristics anywhere — bedroom count, bathroom count, a quality code, a condition code, living-area square footage — the fields Harris's similarity scoring is actually weighted on (living area 24%, bedrooms 14%, bathrooms 12%, quality 10%, age 8%, condition 6%, stories 4%, building character 4%; see `CLAUDE.md`'s Similarity Algorithm section)?

## Finding: yes — already in hand, no external search needed

**Source**: `var/bcad_extracted/2025/2025-07-23_002022_APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT` (real 2025 BCAD certified export, already downloaded; 394,291 rows, uniform 87-char fixed-width lines).

This file encodes building characteristics as attribute-type/attribute-value pairs, one row per (improvement detail, attribute):

| Field | Offset | Notes |
|---|---|---|
| `prop_id` | 0:12 | verified against known real prop_ids |
| `tax_year` | 12:16 | |
| `imp_id` | 16:28 | verified matches a real known `imp_id` for prop `000000010008` (the same test property used throughout the original 5-file decode) |
| (unlabeled linking field) | 28:40 | detail_seq-style, not further decoded in this pass |
| (unlabeled linking field) | 40:52 | not further decoded in this pass |
| `attribute_type` | 52:77 | 25-char, space-padded |
| `attribute_value` | 77:87 | 10-char |

### Attribute-type vocabulary (16 distinct types found)

| Attribute | Row count | Sample values | Use |
|---|---|---|---|
| Plumbing | 43,495 | `2`, `3`, `1`, `2/1`, `4`, `3/1`, `1/1`, `2.5`, `4.5` | **Bathroom count** — `X/Y` = X full + Y half baths, or `X.5` = X full + 1 half |
| Number of Bedrooms | 43,116 | integers 1–12 | **Bedroom count** — directly usable |
| Number of Rooms | 14,832 | integers 1–15 | Total room count |
| Exterior Wall | 38,476 | BV, FR, HS, MT, ST, BK, ... | Material code |
| Foundation | 37,727 | CS, CB, BK, WP | Material code |
| Roof Covering | 36,512 | CP, HP, GA, MT, BU | Material code |
| Heating/Cooling | 35,867 | AH, WU, NO, SP, CA, CH | System code |
| Fireplace | 32,613 | (feature flag, not sampled in detail) | |
| Interior Finish | 30,949 | SR, IN, PR, UN, SW, PN | Finish code |
| Construction Style | 30,822 | FR, SF, CB, SS, TU, RC | Style code |
| Other Feature | 26,160 | free text, e.g. "STORAGE BUILDING" | |
| Flooring | 12,375 | CP, TL, CN, WD, CT (sometimes combined, e.g. "CP, TL") | |
| Covered Patio/Deck, Carport, O/D Kitchen, Pool Attributes, Built-ins | 70–6,605 each | | Feature-presence rows |

**Condition-style rating confirmed to exist in the PACS vocabulary**: `Carport`'s values are `F` (Fair) / `A` (Avg) / `G` (Good) — a Fair/Average/Good scale. No single overall building-wide quality/condition code (matching Harris's X/A/B/C/D grade) was found in this file specifically, but this confirms the PACS system uses this style of rating somewhere in its schema. Worth checking whether it applies more broadly — e.g. inside `APPRAISAL_IMPROVEMENT_INFO.TXT`'s still-ambiguous decimal fields at offsets 69:83/83:98 (flagged as "unclear semantics" during the original decode) — as a natural follow-up, not a blocker to using what's already confirmed.

**Not found in this file**: square footage. Living-area sqft most likely already lives in the already-ingested `APPRAISAL_IMPROVEMENT_DETAIL.TXT`'s `detail_quantity` field for "MAIN AREA"-type component rows (values like `1800.000000` were seen during the original 5-file decode) — this is arguably already captured under `PropertyImprovementDetail`, just not yet aggregated per-improvement into a single square-footage figure.

### Update: sqft, year built, and a class code also confirmed via the GIS parcel shapefile

The sibling research on [wayfinder ticket #4](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/4) (true situs address) downloaded and inspected BCAD's GIS parcel shapefile (`brazoscad.org/tax-information/gis/`, 2025: `BrazosCADParcels_20260422.zip`) and found it also carries, as clean typed attribute fields:

- `living_are` — living area sqft (e.g. `1608`)
- `yr_blt` — year built (e.g. `1991`)
- `class_cd` — a class/quality-style code (e.g. `RF1`, `RF2P`) — likely a quality/class proxy, exact format/meaning not yet confirmed

This fills the one gap left by `APPRAISAL_IMPROVEMENT_DETAIL_ATTR.TXT` above. Combined, Brazos now has a plausible source for nearly everything Harris's similarity weighting needs: living area ✓ (shapefile), bedrooms ✓ (attr file), bathrooms ✓ (attr file), quality ~ (shapefile `class_cd`, format TBD), age ✓ (shapefile `yr_blt`), condition ~ (Fair/Avg/Good vocabulary exists via `Carport`, not yet confirmed as a whole-building rating), building character ✓ (attr file construction/material codes), extra features ✓ (attr file pool/patio/etc). Stories is the one dimension neither source has confirmed yet.

See [wayfinder ticket #7](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/7) (dedicated GIS shapefile characterization) for the authoritative full field list, CRS, and join-key details once it resolves.

## Bottom line

Real, structured, comps-ready data exists today in already-downloaded data. Bedroom count and bathroom count are clean and ready to ingest immediately. Room count and the six construction/material codes (exterior wall, foundation, roof, heating, interior finish, construction style) are usable as building-character signals. A Fair/Average/Good condition scale exists in the vocabulary (via `Carport`) and may generalize further. The GIS parcel shapefile independently confirms living area, year built, and a class code. This did not require checking BCAD's website or considering a public-records request — the primary-source answer from data already on disk (plus one already-public shapefile download) is conclusive.

## Follow-up not covered by this ticket

- Decode the two unlabeled linking fields at offset 28:40/40:52.
- Confirm whether the Fair/Average/Good condition scale generalizes to a whole-building condition/quality rating, possibly via `APPRAISAL_IMPROVEMENT_INFO.TXT`'s offset 69:83/83:98 fields, or via the shapefile's `class_cd`.
- Locate a "stories" field — not confirmed in either source yet.
- Design how these attribute rows (one improvement can have many attribute rows of different types) and the shapefile's fields get modeled/ingested into `brazos_cad` — no model or ingest code changes were made as part of this research ticket.
