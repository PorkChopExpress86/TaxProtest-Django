# ETL Profiling Report

Date: 2026-05-02

## Stage Metrics

| Stage | Flow | Stage Group | Elapsed (s) | CPU Avg % | CPU Peak % | RAM Avg (MiB) | RAM Peak (MiB) | Samples | Exit |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| legacy_property | legacy | property | 186 | 64.08 | 232.39 | 174.33 | 223.90 | 31 | 0 |
| modern_property_stage | modern | property+building+feature | 717 | 78.21 | 226.04 | 877.61 | 1084.42 | 119 | 0 |
| legacy_building | legacy | building+feature | 1453 | 48.55 | 225.99 | 1732.85 | 2159.62 | 241 | 0 |
| legacy_gis | legacy | gis | 1272 | 40.04 | 234.89 | 3626.07 | 3701.76 | 211 | 0 |
| modern_gis_stage | modern | gis | 1366 | 38.60 | 226.30 | 3633.45 | 3697.66 | 227 | 0 |

## Throughput Comparison

- Legacy staged total: **2911s**
- Modern staged total: **2083s**
- Overall winner: **modern** by **828s** (28.44% total)

## In-Stage Timing Breakdown

| Logical Stage | Legacy (s) | Modern (s) | Faster |
|---|---:|---:|---|
| Property load | 186.000 | 331.817 | legacy |
| Building detail load | 164.810 | 268.636 | legacy |
| Extra features load | 106.395 | 112.789 | legacy |
| GIS load/update | 1272.000 | 1366.000 | legacy |

## Result Equivalence (from logs)

- Property records loaded/imported: legacy=1263641, modern=1263641
- Building detail records loaded: legacy=1275023, modern=1275023
- Extra feature records loaded: legacy=997977 (detail1+detail2), modern=938295
- GIS properties updated: legacy=1261051, modern=1261051
- Modern property stage reported failed records: 651509
- Equivalence verdict: **not matched on all counters** (see differing counters above)
