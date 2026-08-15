# HCAD Data Documentation Reference

Summary of the Harris County Appraisal District (HCAD) data files, archives, codebooks, and GIS specifications.

---

## 1. GIS ReadMe

- **Format:** ESRI Shapefile format, compatible with ArcGIS and GIS tools.
- **Coordinate System:** State Plane Texas South Central (Zone 4204), Datum NAD 1983 (EPSG:32039), converted to WGS84 (EPSG:4326) during ingestion.
- **Parcels Shapefile (`ParcelsCity.shp`):**
  - Polygons for every parcel in Harris County.
  - Key field: `HCAD_NUM` (13-digit account number, joins with real property data).
  - `BLK_NUM`, `LOT_NUM`, `CONDO_FLAG` (`1` = stacked for condos/undivided interest).
- **Annotations:**
  - Block number, easement names, lot numbers.
  - Right-of-way annotation for Harris County.

---

## 2. HCAD Archives & Source Data Files

Data files are distributed by HCAD as self-extracting ZIP archives:

| Archive | Key Source Files | Description |
|---|---|---|
| `Real_acct_owner.zip` | `real_acct.txt`, `deeds.txt`, `owners.txt`, `permits.txt`, `real_neighborhood_code.txt` | Core property accounts, owner names, mailing and site addresses, market values. |
| `Real_building_land.zip` | `building_res.txt`, `building_other.txt`, `extra_features_detail1.txt`, `extra_features_detail2.txt`, `extra_features.txt`, `fixtures.txt`, `land.txt` | Residential improvements, square footage, room counts, extra features (pools, garages, sheds), land use. |
| `Real_jur_exempt.zip` | `jur_exempt.txt`, `jur_tax_dist_exempt_value_rate.txt`, `jur_value.txt` | Jurisdictions, tax units, tax rates, and property exemptions. |
| `Real_acct_ownership_history.zip` | `ownership_history.txt` | Historical ownership records. |
| `Hearings_files.zip` | `arb_hearings_real.txt`, `arb_protest_real.txt` | Appraisal Review Board (ARB) hearings and protest records. |
| `Code_description_real.zip` | `desc_r_*.txt` | Lookup descriptions for quality codes, condition codes, building styles, land use, school districts. |
| `Parcels.zip` | `ParcelsCity.shp`, `.dbf`, `.shx`, `.prj` | Shapefiles for parcel boundaries and centroid calculation. |

---

## 3. Key Table Schemas

### `real_acct.txt` (Property Accounts)
- `acct`: 13-digit HCAD account number (Primary Key).
- `str_num`, `str`, `str_sfx`, `site_addr_1`, `site_addr_2`, `site_addr_3`, `zip`: Site address fields.
- `owner_name`: Owner name.
- `tot_mkt_val`, `tot_appr_val`, `assessed_val`: Valuation figures.
- `bld_ar`, `land_ar`: Total building and land areas in square feet.
- `state_class`: State property classification code (e.g. `A1` single family residential).

### `building_res.txt` (Residential Buildings)
- `acct`: Account number.
- `bld_num`: Building sequence number.
- `imprv_type`, `building_style_code`, `bld_cl`: Type, style, and classification codes.
- `quality_cd`: Construction quality (`X`, `A`, `B`, `C`, `D`, `E`, `F`).
- `condition_cd`: Physical condition rating.
- `date_erected`, `yr_remodel`, `eff_yr`: Year built, remodel year, and effective year.
- `heat_ar`: Heated living area (sq ft).

### `fixtures.txt` (Room Counts & Fixtures)
- `acct`, `bld_num`: Account and building number.
- `type`: Fixture code:
  - `RMB`: Bedrooms (units = room count).
  - `RMF`: Full Bathrooms (1.0 each).
  - `RMH`: Half Bathrooms (0.5 each).
  - `STY`: House stories.

### `extra_features_detail1.txt` & `extra_features_detail2.txt`
- `acct`: Account number.
- `dscr`: Human-readable description (`Gunite Pool`, `Frame Detached Garage`, etc.).
- `units`, `length`, `width`, `cond_cd`, `act_yr`, `asd_val`: Dimensions, quantity, condition, year built, and appraised value.

---

## 4. Definitions & Business Rules

- **Account Numbers:** 13-digit numeric string formatted as `XXXXXXXXXXXXX`. Stable unless a parcel is split or merged.
- **Living Area:** `heat_ar` represents the heated/living square footage.
- **Land Use:** `1000` = Residential Vacant, `1001` = Residential Improved. Condominium land is valued proportionally (19% of total market value).
- **Quality Codes:** `X` (Superior) > `A` (Excellent) > `B` (Good) > `C` (Average) > `D` (Low) > `E` (Very Low) > `F` (Poor).
