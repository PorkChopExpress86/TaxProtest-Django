# Protest Analysis Page — Design Spec

**Date:** 2026-04-13
**Status:** Approved for implementation planning

---

## Context

Property tax protest in Harris County (and all of Texas) has two primary legal grounds under Texas Tax Code Chapter 41:

1. **Equity / Equal and Uniform (§41.43):** Your property is appraised at a higher ratio than comparable properties of the same kind and character. The appraisal district must show by preponderance of the evidence that your appraisal ratio is equal to or less than the median of comparable properties. If they can't, the protest is decided in the owner's favor.

2. **Market Value:** Your appraised value exceeds the price a willing buyer would pay a willing seller. This argument requires recent sale prices (MLS data), which is outside current HCAD data.

The existing similarity algorithm (`data/similarity.py`) already identifies comparable properties using the same factors an appraisal professional uses: living area (24%), bedrooms (14%), bathrooms (12%), land size (10%), quality (10%), age (8%), condition (6%), stories (4%), building character (4%), and extra features (4%). The `assessed_value` field is populated for all properties from the HCAD import.

**The gap:** The current `/similar/<account>/` view shows structural similarity but not value comparison. A property owner cannot currently see whether their comparable properties are appraised higher or lower, which is the core of an equity protest.

**This feature** adds a dedicated Protest Analysis page that layers assessed value comparison on top of the existing similarity results — giving property owners evidence-ready output for an ARB hearing.

---

## Feature Summary

A new route `/protest/<account_number>/` presents an equity analysis showing the subject property's assessed value per square foot compared to the median of its most similar neighbors. A configurable similarity threshold filters out weak comparables. The page is print-optimized and includes a CSV export for HCAD's online portal.

---

## URLs

| URL | Method | Purpose |
|---|---|---|
| `/protest/<account_number>/` | GET | Protest analysis page |
| `/protest/<account_number>/export/` | GET | CSV export of comparable properties table |

`account_number` is a string (matches `PropertyRecord.account_number`).

Query parameters for both URLs:
- `min_score` — float, minimum similarity score for comparables; default `70.0`; clamped to range `[52.0, 100.0]`

---

## View Logic

### `protest_analysis(request, account_number)` — `taxprotest/views.py`

1. Fetch `PropertyRecord` for `account_number` with `is_residential=True, is_data_ready=True` (or 404).
2. Fetch associated `BuildingDetail` (or handle gracefully if missing).
3. Parse `min_score = float(request.GET.get('min_score', 70.0))`, clamp to `[52.0, 100.0]`.
4. Call `find_similar_properties(account_number, max_distance_miles=10.0, max_results=50, min_score=min_score)`.
5. Filter comps: include only results where both `assessed_value` and `heat_area` are non-null and non-zero.
6. Compute subject metrics:
   - `subject_value_per_sqft = subject.assessed_value / subject_heat_area` (only if both are available)
7. For each qualifying comp, compute:
   - `comp_value_per_sqft = comp['assessed_value'] / comp['heat_area']`
   - `comp_delta = comp_value_per_sqft - subject_value_per_sqft` (positive = comp assessed higher than subject)
8. Compute equity summary (only if subject $/sqft is available and at least 1 comp qualifies):
   - `median_comp_value_per_sqft` = statistical median of all comp $/sqft values
   - `equity_gap_per_sqft = subject_value_per_sqft - median_comp_value_per_sqft`
   - `estimated_savings = max(0, equity_gap_per_sqft * subject_heat_area)`
   - `comps_below_subject = count(comps where comp_value_per_sqft < subject_value_per_sqft)`
9. Pass all data to `templates/protest_analysis.html`.

**Edge cases:**
- Subject has no `assessed_value` or no `heat_area`: show subject card, skip equity summary, show comps table without delta column.
- No qualifying comps found: show subject card and equity summary (empty), show a "no qualifying comparables found" message with a suggestion to lower the threshold.
- Subject is not found or not residential/data-ready: 404.

### `protest_analysis_export(request, account_number)` — `taxprotest/views.py`

Same logic as `protest_analysis` through step 7, then return a `StreamingHttpResponse` with `Content-Type: text/csv` and filename `protest_analysis_{account_number}.csv`.

CSV columns: `address, similarity_score, similarity_label, living_area_sqft, bedrooms, bathrooms, year_built, quality_code, condition_code, assessed_value, value_per_sqft, delta_vs_subject_per_sqft`

---

## Template: `templates/protest_analysis.html`

Extends `base.html`. Uses Bootstrap 5.

### Sections (top to bottom)

**1. Subject Property Card**
Bootstrap card showing:
- Address (large heading)
- Account number
- Assessed value (formatted as currency)
- Value per sqft (if computable)
- Living area (sqft)
- Bedrooms / Bathrooms
- Year built
- Quality code / Condition code

**2. Equity Summary Banner**
Bootstrap alert/callout. Two states:

- **Overassessed** (equity_gap > 0): `alert-warning` color.
  > "Your property is assessed at **$185/sqft** — **$23/sqft above** the median of **8 comparable properties**. Estimated equity protest savings: **$46,000**."

- **At or below median** (equity_gap ≤ 0): `alert-success` color.
  > "Your property's assessed value per sqft is at or below the median of comparable properties. An equity protest may not be the strongest argument for this property."

- **Insufficient data**: `alert-secondary` — explain which data is missing.

**3. How to Use This Analysis (Bootstrap Accordion, collapsed by default)**

Three panels:
- **Equity Protest (Texas §41.43):** Explains "equal and uniform" standard, that this report's comparable table is direct equity protest evidence, and that the ARB hearing requires evidence exchange 5 days in advance.
- **Market Value Protest:** Explains this is a separate ground requiring recent sale prices (MLS data). This tool uses HCAD assessed values as a proxy — useful for context but not a direct sale-price argument. Recommend supplementing with MLS comps for a market value argument.
- **Submitting Your Evidence:** Bring 3 printed copies of this report to the ARB hearing. Submit evidence online at HCAD's protest portal at least 5 days before the hearing date. Include property photos if condition is a factor.

**4. Controls Row**

Left side: Minimum similarity score slider (Bootstrap range input, `min=52, max=100, step=1`, default `70`). Displays current value inline. On change: reloads the page with updated `?min_score=` query parameter.

Right side: Two buttons — "Print Report" (`onclick="window.print()"`) and "Download CSV" (links to `/protest/<account>/export/?min_score=<current>`).

**5. Comparable Properties Table**

Bootstrap responsive table, sorted by similarity score descending.

Columns:
| Column | Notes |
|---|---|
| Address | Linked to `/similar/<account>/` page of that comp |
| Score | Numeric score + similarity label badge (Bootstrap badge, color-coded by label) |
| Sqft | Living area |
| Bed / Bath | Combined cell |
| Year Built | |
| Assessed Value | Formatted as currency |
| $/sqft | Formatted as currency |
| vs. Yours | Delta (comp $/sqft minus subject $/sqft). Positive = comp higher (green). Negative = comp lower (red). |

If subject $/sqft is unavailable, omit the last two columns.

Empty state: "No comparable properties found at this similarity threshold. Try lowering the minimum score slider."

**6. Legal Disclaimer**

Small muted text at bottom:
> "This analysis uses Harris County Appraisal District (HCAD) data and the property similarity algorithm on this site. It is for informational purposes only and does not constitute legal or appraisal advice. Assessed values are from HCAD records and may not reflect current market sale prices. Consult a licensed property tax consultant or appraiser for professional advice."

---

## Print CSS

`@media print` rules (inline `<style>` block in the template):
- Hide: `navbar`, `footer`, controls row (slider + buttons), accordion (keep it collapsed/hidden), legal disclaimer.
- Show: Subject card, equity summary banner, table (full width).
- Force page break before table if equity summary is tall.
- Set font to serif for readability.
- Add printed header line: "Harris County Property Tax Protest Analysis — [address] — Printed [date]"

---

## Files Modified / Created

| File | Action | Change |
|---|---|---|
| `taxprotest/views.py` | Modify | Add `protest_analysis` and `protest_analysis_export` views |
| `taxprotest/urls.py` | Modify | Add 2 URL patterns |
| `templates/protest_analysis.html` | Create | New template |
| `templates/similar_properties.html` | Modify | Add "Protest Analysis" button near top of page |

No model changes. No changes to `data/similarity.py`.

---

## Key Reused Utilities

- `data.similarity.find_similar_properties()` — called unchanged
- `statistics.median()` (Python stdlib) — for median comp $/sqft calculation
- Existing Bootstrap 5 layout from `base.html`
- Existing `export_csv` pattern in `taxprotest/views.py` for CSV streaming response structure

---

## Verification

1. Navigate to `/protest/<account_number>/` for a known residential property.
2. Confirm subject card shows correct address, assessed value, and $/sqft.
3. Confirm equity banner shows correctly (overassessed vs. at/below median).
4. Adjust the slider — verify the table and banner update to reflect fewer/more comps.
5. Click "Print Report" — verify navbar and controls are hidden, table is clean.
6. Click "Download CSV" — verify file downloads with correct columns and data.
7. Confirm "How to Use" accordion is collapsed by default and opens correctly.
8. Navigate to a property with no `assessed_value` — confirm graceful degradation (no delta column, no equity banner).
9. Navigate to a non-existent account — confirm 404.
10. Run `docker compose exec web python manage.py test` — all existing tests must pass.
