# Property Similarity Scoring Guide

The similarity search finds and ranks comparable properties for protest research and evidence
reports. Decisions behind its shape are recorded in
[ADR-0021](../adr/0021-similarity-math-is-shared.md).

It has two layers:

1. **Shared math** ([`counties/common/similarity_math.py`](../../counties/common/similarity_math.py)):
   numeric helpers, curve interpolation, the per-factor similarity functions (percentage
   difference, absolute difference, categorical code, ranked code, distance), the tuning curves as
   named constants, the label bands, score assembly, and `nearby_properties`, the one
   nearest-properties query. It names no county model.
2. **County scorers** ([`counties/harris/similarity.py`](../../counties/harris/similarity.py),
   [`counties/brazos/similarity.py`](../../counties/brazos/similarity.py)): each county owns its
   factor list and weights, quality and condition semantics, feature overlap, candidate
   pre-filters, and pair scorer, and exposes `find_similar_properties`.

The tuning curves are shared. A county that needs a different curve defines and justifies its own
constant ([ADR-0003](../adr/0003-county-etl-parity-is-source-aware.md)) instead of editing the
shared one.

---

## 1. Search workflow

1. **Subject lookup.** The county loads the subject and returns no results when it is missing or
   has no coordinates. Harris requires `is_residential=True` and `is_data_ready=True`; Brazos
   looks the `prop_id` up in the requested tax year.
2. **County pre-filters.** The county builds the candidate queryset and excludes the subject:
   - Harris: residential, data-ready records. When the subject's active building has a heated
     area, candidates must have an active building with 50%-150% of it (the Harris living-area
     window).
   - Brazos: properties in the same tax year. There is no living-area window.
3. **Nearest properties.** `nearby_properties` adds a latitude/longitude bounding box, annotates a
   great-circle `distance` in miles computed in the database (spherical law of cosines, earth
   radius 3,959 miles), keeps candidates within `max_distance_miles`, orders them nearest first,
   and keeps at most `NEARBY_PROPERTIES_CAP` (2,000). This is the only distance calculation; there
   is no Python-side distance helper.
4. **Facts.** Harris bulk-loads candidates' active buildings and active extra features. Brazos
   loads the subject's and every candidate's scoring facts (primary improvement, characteristics,
   features, acreage, stories) with a fixed number of queries, keyed by property and improvement
   identifier together because BCAD repeats `imp_id` across properties. Neither county queries
   per pair.
5. **Scoring.** Each pair is scored in Python. Results below `min_score` are dropped.
6. **Selection.** Results are sorted by score descending, then distance ascending, then the
   county key (Harris `account_number`, Brazos `prop_id`), and truncated to `max_results`. The
   shared web layer owns display order.

Each result is a dictionary with `property`, `building`, `features`, `distance` (rounded to two
decimals), `similarity_score` and `score_breakdown`. Brazos results also carry `acreage`.

---

## 2. Residential components

Points are the maximum each component contributes.

| Component | Harris | Brazos | Shared function and curve |
|---|---:|---:|---|
| Living area | 24 | 24 | percentage difference, `LIVING_AREA_CURVE` |
| Bedrooms | 14 | 14 | absolute difference, `BEDROOMS_CURVE` |
| Bathrooms | 12 | 12 | absolute difference, `BATHROOMS_CURVE` |
| Land size | 10 | 10 | percentage difference, `LAND_SIZE_CURVE` |
| Quality | 10 | 16 | rank difference, `RANK_DIFFERENCE_CURVE` |
| Condition | 6 | - | rank difference, else categorical code |
| Age | 8 | 8 | absolute difference, `AGE_CURVE` |
| Stories | 4 | 4 | absolute difference, `STORIES_CURVE` |
| Building character | 4 | 4 | categorical code |
| Features | 4 | 4 | county-owned set overlap |
| Distance | 4 | 4 | fraction of the radius, `DISTANCE_CURVE` |

Both weight sets total 100. Brazos has no whole-building condition rating separate from quality,
so it folds Harris's quality and condition weights into one 16-point quality component.

### Data sources

| Component | Harris | Brazos |
|---|---|---|
| Living area | `BuildingDetail.heat_area` | `PropertyAccount.living_area` |
| Bedrooms, bathrooms | `BuildingDetail.bedrooms`, `.bathrooms` | `PropertyBuildingCharacteristic.bedrooms`, `.bathrooms` |
| Land size | `PropertyRecord.land_area` | sum of `PropertyLand.acreage` |
| Quality | `BuildingDetail.quality_code`, ranked `X=7, A=6, B=5, C=4, D=3, E=2, F=1` | first digit of `PropertyAccount.class_code` |
| Condition | `BuildingDetail.condition_code` | - |
| Age | first of `effective_year`, `year_remodeled`, `year_built` | primary improvement `year_built`, else `PropertyAccount.year_built` |
| Stories | `BuildingDetail.stories` | 2 when the primary improvement has a `SECOND FLOOR` detail row, else 1 |
| Building character | first comparable of `building_style`, `building_type`, `building_class` | first comparable of `exterior_wall`, `construction_style`, `foundation` |
| Features | active `ExtraFeature.feature_code` | `PropertyExtraFeature.feature_type` |

Feature overlap is the number of shared codes divided by the number of distinct codes on either
side; it is unavailable when neither side has any.

The Brazos primary improvement is the first residential (`improvement_type='R'`) improvement, by
`imp_id`, that has a characteristics row, else the first residential improvement.
`primary_improvement` is the public helper for it. Brazos readiness keeps its own
primary-improvement rule (ADR-0020).

---

## 3. Building-free scoring

When neither property has building facts (Harris: no active building; Brazos: no characteristics
row on either primary improvement), only three components are scored, with land-only weights:

| Component | Points |
|---|---:|
| Land size | 80 |
| Features | 10 |
| Distance | 10 |

---

## 4. Score assembly

Each available component has a similarity `s` in `[0, 1]` and earns `weight x s` points. An
unavailable component (missing data on either side) earns nothing and is left out of the base
score:

```
base_score   = sum(weight x s over available) / sum(weight over available)
coverage     = sum(weight over available) / sum(weight over all components)
multiplier   = 0.8 + 0.2 x coverage      (1.0 for building-free scoring)
final_score  = base_score x multiplier x 100, clamped to 0-100, rounded to one decimal
```

Missing data therefore lowers a score by up to 20% instead of inflating confidence.

---

## 5. Labels

`get_similarity_label` applies one set of bands to both counties:

| Score | Label |
|---|---|
| 84 and above | Best match |
| 70 to below 84 | Highly similar |
| 52 to below 70 | Good match |
| 36 to below 52 | OK match |
| below 36 | Broad match |

The comparables page defaults to `min_score=30`, so Broad matches from 30 up are shown. The
protest report defaults to a minimum score of 70.

---

## 6. Recorded county differences

These differences are recorded in ADR-0021 as unclassified rather than source-justified. Each
needs its own approved change.

- **Building-free scoring.** Brazos switches to land-only weights whenever neither side has a
  characteristics row, even for improved properties.
- **Harris half-building rule.** When only one side has an active building, Harris keeps the
  residential weights but skips every residential component, so only land size, features and
  distance score, at their residential weights.
- **Harris living-area window.** Only Harris pre-filters candidates to 50%-150% of the subject's
  heated area.
- **Two Harris primary-building rules.** The subject uses the first active building the database
  returns; candidates use the last active building loaded for the account.
- **Eligibility filtering after truncation.** The Brazos comparables lookup in
  `counties/brazos/adapter.py` drops peers that are not comparable-ready or not in the subject's
  comparison mode after the search has already truncated to `max_results`, so it can return fewer
  than `max_results`.

---

## 7. Using the search

```python
from counties.harris.similarity import find_similar_properties

results = find_similar_properties(
    "0123456789012",
    max_distance_miles=10.0,
    max_results=50,
    min_score=30.0,
)

# Brazos also takes the tax year:
# from counties.brazos.similarity import find_similar_properties
# results = find_similar_properties("123456", tax_year=2025)

for result in results:
    print(result["similarity_score"], result["distance"])
    for component in result["score_breakdown"]:
        if component["available"]:
            print(component["label"], component["points"], component["weight"])
```

The shared web surface reaches the search through each county's `CountyAdapter`:

- Comparables page: `/similar/<key>/` (Harris), `/brazos/similar/<key>/` (Brazos).
- Protest report: `/protest/<key>/` and `/brazos/protest/<key>/`, with CSV and PDF exports under
  `export/` and `pdf/`.
- Search CSV export: `/export/` for counties whose profile supports it.

---

## Tests

- `counties/common/tests/test_similarity_math.py`: the shared math through its public functions.
- `counties/common/tests/test_similarity_golden.py`: golden results for both counties' searches
  and comparables lookups; every refactor must keep them unchanged.
- `counties/harris/tests/test_similarity_scoring.py`, `counties/brazos/tests/test_similarity.py`:
  county scoring through each county's public search.
- `counties/common/tests/test_similarity_boundary.py`: no module imports a private similarity name.
