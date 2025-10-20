# Bedroom/Bathroom Data Issue - Investigation Results

**Date:** October 16, 2025  
**Issue:** Properties on Wall Street (and all properties) don't show bedroom/bathroom counts

## Root Cause

**HCAD does not provide bedroom and bathroom counts in their public data files.**

### Investigation Summary

1. **Building Records Exist:** The `BuildingDetail` records are successfully imported (1.3M records)
2. **Fields Are NULL:** The `bedrooms` and `bathrooms` fields are NULL in the database
3. **Source Data Missing:** The source files from HCAD don't contain this data

### Files Checked

**building_res.txt:**
- ✅ Contains: area measurements (heat_ar, gross_ar, base_ar, etc.)
- ✅ Contains: year built, year remodeled
- ✅ Contains: building type, style, class
- ❌ Does NOT contain: bedroom or bathroom counts

**structural_elem1.txt & structural_elem2.txt:**
- ✅ Contains: Foundation type, heating/AC, condition codes
- ❌ Does NOT contain: Room counts (RMB, RMF, RMH codes not found)

**Verification:**
```bash
# Checked for bedroom codes in structural elements
grep -E "RMB|RMF" structural_elem1.txt  # No results
grep -E "RMB|RMF" structural_elem2.txt  # No results
```

## Why This Happens

HCAD's public data files are designed for property valuation and tax purposes, not for real estate listings. They focus on:
- **Valuation metrics:** Area, year built, quality codes
- **Structural details:** Foundation, roof, exterior
- **Extra features:** Pools, garages, patios

They **do not include** typical real estate details like:
- Number of bedrooms
- Number of bathrooms  
- Interior layout details
- Appliances or finishes

## Current State

### Database
- **1,300,814** BuildingDetail records imported
- **bedrooms field:** NULL for all records
- **bathrooms field:** NULL for all records
- **Other fields:** Populated correctly (heat_area, year_built, etc.)

### UI Display
The table currently shows:
```
Bed/Bath Column: "- / -" (no data)
```

## Solutions

### Option 1: Remove Bed/Bath Column (Recommended)
Since HCAD doesn't provide this data, remove the column from the table.

**Pros:**
- Honest about data availability
- Cleaner table without confusing "-" values
- No user expectations of bedroom/bathroom data

**Cons:**
- Less feature-rich than expected

### Option 2: Add Data Source Disclaimer
Keep the column but add a note explaining data limitations.

**Pros:**
- Column structure remains consistent
- Users understand why data is missing

**Cons:**
- Clutters UI with empty columns
- May confuse users

### Option 3: Integrate Third-Party Data Source
Obtain bedroom/bathroom data from another source (Zillow API, HAR, etc.)

**Pros:**
- Complete property information
- More useful for home buyers

**Cons:**
- Requires API keys and costs
- Data synchronization complexity
- Legal/licensing considerations
- Not all properties may match

### Option 4: Manual Data Entry/Crowdsourcing
Allow users to add/edit bedroom/bathroom counts.

**Pros:**
- Community-driven data improvement
- Could eventually build complete dataset

**Cons:**
- Data quality concerns
- Requires authentication and moderation
- Significant development effort

## Recommendation

**Remove the Bed/Bath column** from the search results table and CSV export since the data is not available from HCAD. Focus on the data that IS available:

**Available Property Data:**
- ✅ Building area (square footage)
- ✅ Land area
- ✅ Year built
- ✅ Assessed value
- ✅ Extra features (pool, garage, etc.)
- ✅ Location (lat/long)
- ✅ Building type, style, quality

This data is still very useful for:
- Property valuation comparisons
- Tax protest preparation
- Finding similar properties by size and features
- Market analysis

## Alternative: Use Building Area as Proxy

Without bedroom counts, users can still estimate property size using:
- **Heat Area (sqft):** Available for all properties
- **General guidelines:**
  - <1,200 sqft: Likely 1-2 bedrooms
  - 1,200-2,000 sqft: Likely 2-3 bedrooms  
  - 2,000-3,000 sqft: Likely 3-4 bedrooms
  - 3,000+ sqft: Likely 4+ bedrooms

## Implementation Plan

1. **Remove bed/bath column** from templates/index.html
2. **Remove bed/bath fields** from views.py (index and export_csv)
3. **Update documentation** to clarify available data fields
4. **Consider future:** If third-party data becomes available, can add back

## Files to Update

- `templates/index.html` - Remove Bed/Bath column
- `taxprotest/views.py` - Remove bedrooms/bathrooms queries
- `TABLE_FEATURE_COLUMNS_UPDATE.md` - Update documentation

---

**Status:** Investigation complete - HCAD data does not include bedroom/bathroom counts
**Next Step:** Decide whether to remove column or pursue alternative data source
