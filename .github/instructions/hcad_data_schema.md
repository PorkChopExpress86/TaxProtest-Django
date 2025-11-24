# HCAD Data Schema and Relationships

This document describes how the Harris County Appraisal District (HCAD) real property dataset is organized. It explains what each file contains, the recommended PostgreSQL schema (primary keys and relationships), and which files to import for building a local database.

---

## 1. Overview

The HCAD data is organized into several families of files:

1. **Parcel & Ownership Data** — Core property information, ownership, and deeds.  
2. **Buildings & Land** — Structural, land, and physical characteristics.  
3. **Jurisdictions & Exemptions** — Taxing jurisdictions, exemptions, and values.  
4. **Code Description Tables** — Lookup tables for decoding categorical fields.  
5. **Hearing/Protest Data (optional)** — Historical ARB and protest data.

The universal key across all datasets is the **13-digit account number** (`acct`), which uniquely identifies a parcel.

---

## 2. Parcel & Ownership Tables

### `real_acct`
**Purpose:** Primary parcel table (one record per account).

**Fields:** Account ID, owner, mailing/site addresses, legal description, neighborhood, market area, school district, land and building values, total market/appraised values, and classification codes.

**Primary key:** `acct`

### `owners`
**Purpose:** Multiple owners per parcel, with percent ownership.

**Primary key:** `(acct, owner_seq)`  
**Foreign key:** `acct → real_acct.acct`

### `deeds`
**Purpose:** Deed/sale records per parcel.

**Primary key:** `(acct, deed_id)`  
**Foreign key:** `acct → real_acct.acct`

### `permits`
**Purpose:** Construction and remodel permit data.

**Primary key:** `(acct, permit_id)`  
**Foreign key:** `acct → real_acct.acct`

### `parcel_tieback`
**Purpose:** Relationships between parcels (splits, mergers, UDIs).

**Primary key:** `(acct, related_acct, relationship_type)`  
**Foreign keys:** `acct`, `related_acct → real_acct.acct`

### `real_neighborhood_code`
**Purpose:** Neighborhood and group descriptions.

**Primary key:** `neighborhood_code`

---

## 3. Buildings, Land, and Physical Characteristics

### `building_res`
**Purpose:** Residential building data.

**Fields:** `acct`, `bldg_num`, use code, building type, style, class, quality, year built, effective year, remodel year, area (base, gross, heated, effective), percent complete, replacement cost.

**Primary key:** `(acct, bldg_num)`  
**Foreign key:** `acct → real_acct.acct`

### `building_other`
**Purpose:** Commercial or income property buildings.

**Primary key:** `(acct, bldg_num)`  
**Foreign key:** `acct → real_acct.acct`

### `fixtures`
**Purpose:** Per-building features (bedrooms, baths, stories, fireplaces, etc.).

**Primary key:** `(acct, bldg_num, fixture_type)`  
**Foreign key:** `(acct, bldg_num) → building_res(acct, bldg_num)`

### `exterior`
**Purpose:** Subareas (garages, porches, patios, etc.).

**Primary key:** `(acct, bldg_num, subarea_type, subarea_seq)`  
**Foreign key:** `(acct, bldg_num) → building_res`

### `extra_features` / `extra_features_detail1/2`
**Purpose:** Additional structures/features (pools, decks, sheds, etc.) with detailed valuation.

**Primary key:** `(acct, feature_id)`  
**Foreign key:** `acct → real_acct`

### `land`
**Purpose:** Non-agricultural land segments.

**Fields:** Land use codes, unit type (sqft, acres), size, influence factors, condition, price, and land value.

**Primary key:** `(acct, land_seq)`  
**Foreign key:** `acct → real_acct`

### `land_ag`
**Purpose:** Agricultural/timber land.

**Primary key:** `(acct, land_seq)`  
**Foreign key:** `acct → real_acct`

### `structural_elem1` / `structural_elem2`
**Purpose:** Structural elements for residential and commercial properties respectively.

**Primary key:** `(acct, bldg_num, element_type)`  
**Foreign key:** `(acct, bldg_num) → building_res`

---

## 4. Jurisdictions, Exemptions, and Values

### `jur_exempt`
**Purpose:** Exemptions by account and jurisdiction.

**Primary key:** `(acct, jurisdiction_code, exemption_code)`  
**Foreign key:** `acct → real_acct`

### `jur_exempt_cd`
**Purpose:** Exemption codes per account.

**Primary key:** `(acct, exemption_code)`  
**Foreign key:** `acct → real_acct`

### `jur_exemption_dscr`
**Purpose:** Descriptions for exemption codes.

**Primary key:** `(jurisdiction_code, exemption_code)`

### `jur_tax_dist_exempt_value_rate`
**Purpose:** Exemption values and tax rates by jurisdiction.

**Primary key:** `(jurisdiction_code, exemption_code)`

### `jur_value`
**Purpose:** Appraised, capped, and taxable values per jurisdiction.

**Primary key:** `(acct, jurisdiction_code)`  
**Foreign key:** `acct → real_acct`

---

## 5. Code Description Tables

All files from `Code_description_real.zip` provide lookup data for coded fields.

| Table | Description |
|--------|-------------|
| `desc_r_01_state_class` | Property use / state class |
| `desc_r_02_building_type_code` | Building type |
| `desc_r_03_building_style` | Building style |
| `desc_r_04_building_class` | Building class |
| `desc_r_05_building_data_elements` | Fixture/element codes |
| `desc_r_06_structural_element_type` | Structural element types |
| `desc_r_07_quality_code` | Quality codes |
| `desc_r_09_subarea_type` | Subarea (exterior) codes |
| `desc_r_10_extra_features` | Extra feature types |
| `desc_r_11_extra_feature_category` | Feature categories |
| `desc_r_12_real_jurisdictions` | Jurisdiction names |
| `desc_r_14_exemption_category` | Exemption categories |
| `desc_r_15_land_usecode` | Land use codes |
| `desc_r_20_school_district` | School districts |
| `desc_r_21_market_area` | Market areas |
| `desc_r_25_conclusion_code` | ARB conclusion codes |
| `desc_r_26_neighborhood_num_adjust` | Neighborhood adjustments |

All of these tables use a single-column primary key corresponding to their code.

---

## 6. Hearing and Protest Data (optional)

| Table | Description | Primary Key |
|--------|-------------|--------------|
| `arb_hearings_real` | ARB hearings by account and year | `(acct, tax_year, hearing_id)` |
| `arb_protest_real` | Protest filings | `(acct, tax_year, protest_id)` |

---

## 7. File Import Mapping (Recommended for PostgreSQL)

**From `Real_acct_owner.zip`:**
- real_acct.txt → real_acct  
- owners.txt → owners  
- deeds.txt → deeds  
- permits.txt → permits  
- parcel_tieback.txt → parcel_tieback  
- real_neighborhood_code.txt → real_neighborhood_code

**From `Real_building_land.zip`:**
- building_res.txt → building_res  
- fixtures.txt → fixtures  
- exterior.txt → exterior  
- extra_features*.txt → extra_features / details  
- land.txt → land  
- structural_elem1.txt → structural_elem1

**From `Real_jur_exempt.zip`:**
- jur_exempt.txt → jur_exempt  
- jur_value.txt → jur_value

**From `Code_description_real.zip`:**
- All desc_r_*.txt files → lookup tables

**Optional:** `arb_hearings_real.txt`, `arb_protest_real.txt`

---

## 8. Relationship Summary

- `real_acct` is the **hub** table.  
- Every other table references it by `acct`.  
- Buildings (`building_res`) connect to fixtures, exterior, and structural elements via `(acct, bldg_num)`.  
- Land and land_ag connect directly by `acct`.  
- Extra features, permits, and deeds all link to `acct`.  
- Lookup tables (`desc_r_*`) decode categorical fields from other tables.  
- Jurisdiction tables connect `acct` to taxing district codes.

---

## 9. Suggested Database Schema Graph

```
real_acct ───< building_res ───< fixtures
     │              │
     │              ├──< exterior
     │              └──< structural_elem1
     │
     ├──< land
     ├──< extra_features
     ├──< owners
     ├──< deeds
     ├──< permits
     ├──< jur_value
     └──< jur_exempt
```

---

## 10. Typical Query Joins for Comparable Homes

```sql
SELECT
    ra.acct,
    ra.site_addr_1,
    ra.neighborhood_code,
    br.year_built,
    br.heat_ar AS living_sqft,
    fx_bed.fixture_units AS bedrooms,
    fx_bath.fixture_units AS bathrooms,
    ln.units AS lot_sqft,
    ra.total_mkt_val
FROM real_acct ra
LEFT JOIN building_res br ON ra.acct = br.acct AND br.bldg_num = '1'
LEFT JOIN fixtures fx_bed ON ra.acct = fx_bed.acct AND fx_bed.fixture_type = 'BED'
LEFT JOIN fixtures fx_bath ON ra.acct = fx_bath.acct AND fx_bath.fixture_type = 'BTH'
LEFT JOIN land ln ON ra.acct = ln.acct
WHERE ra.neighborhood_code = '12345';
```

---

**End of File**

