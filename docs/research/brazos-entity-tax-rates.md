# Brazos taxing-entity adopted tax rates

Research for [wayfinder ticket #8](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/8) on the [Brazos County tax-analysis parity map](https://github.com/PorkChopExpress86/TaxProtest-Django/issues/3).

## Question

Where does Brazos County publish adopted tax rates per taxing entity — mirroring how Harris sources data for `import_tax_unit_rates`?

## Finding: not in the certified export — found externally, and simpler than Harris's own source

### Not in the certified export

Re-examined both entity-related files in `var/bcad_extracted/2025/`:

- `APPRAISAL_ENTITY_TOTALS.TXT` (43 rows, 2140 chars/line) — per-entity certified value/exemption totals (a Truth-in-Taxation rollup), not rates. Confirmed via `entity_id` (0:12) / `entity_code` (12:17) / `entity_name` prefix followed by a long sequence of 15-digit zero-padded dollar totals (2 implied decimals). No field in the 0.4–2.0 decimal range consistent with a tax rate.
- `APPRAISAL_ENTITY_INFO.TXT` (755,602 rows, 2750 chars/line) — a per-property × per-entity value/exemption breakdown (the Harris `PropertyJurisdictionExemption` equivalent). Same pattern — dense value totals and exemption-count flags, no rate field.

Both are the certified-values-to-entities step of Truth-in-Taxation, which precedes rate adoption — rates get set later by each entity's own budget process, so structurally they wouldn't be in BCAD's property data export at all.

### Found externally — BCAD itself publishes it, not the county tax assessor-collector

**URL**: `https://brazoscad.org/tax-information/adopted-tax-rates/`
**Format**: plain HTML table — columns Entity Code | Entity Name | M&O | I&S | Total Rate.

Real 2025 example rows (entity codes match `APPRAISAL_ENTITY.TXT`'s `entity_code` field exactly — clean join key):

| Entity Code | Entity Name | M&O | I&S | Total Rate |
|---|---|---|---|---|
| G1 | Brazos County | $0.389454 | $0.030246 | $0.419700 |
| S1 | Bryan ISD | $0.676900 | $0.270000 | $0.946900 |
| S2 | College Station ISD | $0.696300 | $0.279000 | $0.975300 |
| C1 | City of Bryan | $0.452846 | $0.171154 | $0.624000 |

15 entities total listed. `Total Rate = M&O + I&S`, spot-checked and sums correctly on every row.

## Recommendation

This is a lower-effort source than Harris's `import_tax_unit_rates` TSV-upload pattern — it's a static, scrapable HTML table on a page BCAD already controls, likely simpler to build a loader for (scrape → parse table → join on `entity_code`) than a file-upload workflow. Per the map's Notes, this ticket is lower priority than the others — tax impact calculations can degrade gracefully (`completeness="missing"`, the existing pattern in `data/tax_impact.py`) until this is implemented.

`APPRAISAL_ENTITY.TXT` decoding itself was not touched here — it's already resolved elsewhere (see the original 5-file decode); this ticket was locate-only, no ingestion command was designed or built.
