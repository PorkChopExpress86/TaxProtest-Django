# Texas statewide parcel GIS as a Brazos fallback

Checked 2026-08-24 (America/Chicago) against first-party Texas Geographic
Information Office (TxGIO/TWDB), TxDOT-hosted TxGIO service metadata, and
Brazos Central Appraisal District (BCAD) sources only.

## Question

Can an official Texas statewide parcel GIS source replace BCAD's missing 2026
certified GIS source, or can BCAD's explicitly 2025-certified shapefile safely
enrich 2026 CAD accounts with locations?

## Finding

**Texas does publish an official statewide parcel aggregation, but it is not a
2026-certified BCAD replacement and must not pass through the annual-refresh
path.** TxGIO is a Texas Water Development Board division and its [Land
Parcels program](https://geographic.texas.gov/stratmap/land-parcels.html)
publishes statewide shapefile/geodatabase data at no cost. Its data come from
county appraisal districts or their vendors and are translated into a common
schema; TxGIO says it does not edit the geometry.

The state [collection record](https://api.tnris.org/api/v1/collections/0fa04328-872e-481c-b453-126a74777593)
identifies the release as 2025, records an acquisition date of **2025-06-01**,
and lists **Brazos** among its counties. It provides direct Brazos [Shapefile](https://tnris-data-warehouse.s3.us-east-1.amazonaws.com/LCD/collection/stratmap-2025-land-parcels/items/shp/stratmap-2025-land-parcels-brazos_48041_shp.zip)
and [file geodatabase](https://tnris-data-warehouse.s3.us-east-1.amazonaws.com/LCD/collection/stratmap-2025-land-parcels/items/fgdb/stratmap-2025-land-parcels-brazos_48041_fgdb.zip)
downloads. The catalog's license field is unset; TxGIO's published guidance
says the data are not survey-grade and should not be used for legal purposes.

| Requirement | Statewide TxGIO result |
| --- | --- |
| Official state source | Yes. TxGIO is a [TWDB division](https://geographic.texas.gov/about). |
| Parcel geometry and attributes | Yes. The [public layer schema](https://feature.geographic.texas.gov/arcgis/rest/services/Parcels/stratmap_land_parcels_48_most_recent/MapServer/0) exposes parcel geometry, `PROP_ID`, `TAX_YEAR`, `DATE_ACQ`, `SOURCE`, county/FIPS, and situs fields. |
| Join identity | Potentially. TxGIO's [parcel schema](https://cdn.tnris.org/documents/tnris-land-parcel-schema.pdf) defines `PROP_ID` as a source unique identifier that can join other datasets. It does **not** guarantee that a particular county's values are complete or match a later BCAD CAD export. |
| Brazos coverage / exact BCAD values | Coverage is confirmed: the state catalog lists Brazos and provides county-specific SHP/GDB downloads. Exact `PROP_ID` values, match rate, duplicates, and null rate against the BCAD 2026 CAD export remain **unverified**. |
| Access / license | Public, no-cost download. The state catalog supplies no license value; TxGIO describes the dataset as non-survey-grade and unsuitable for legal use. |
| Currency / annual snapshot | Not suitable. TxGIO says county refresh rates vary, data are never a completed/final statewide version, and a county may have newer or more complete data at its appraisal district. |

## Implication for the 2026 annual refresh

Do **not** substitute either TxGIO's 2025 aggregation or BCAD's explicitly
2025-certified shapefile in `refresh_brazos_annual` for a 2026 CAD rebuild.
The application defines an annual refresh as a *year-matched certified CAD and
GIS snapshot*, and validates both source years before either persistence stage.
Calling a mixed-source result a 2026 annual snapshot would break that contract,
even if many parcel centroids happen to be unchanged.

BCAD's [GIS catalog](https://brazoscad.org/tax-information/gis/) still labels
its newest certified archive as **2025**, while the [CAD catalog](https://brazoscad.org/certified-data-downloads/)
offers 2026 certified data. A parcel identifier or geometry can also change
when accounts are split, combined, or remapped; this check did not establish
that 2025 `PROP_ID` coverage is complete for the 2026 CAD export.

## Narrow alternative: coordinate-only enrichment

A **separate, explicitly non-annual** coordinate-only enrichment could be
considered after validation. It must not be presented as a 2026 certified GIS
snapshot and must:

1. retain the actual source label and acquisition/effective date;
2. prove the selected source contains Brazos records and calculate the
   normalized `PROP_ID` match, unmatched, and duplicate rates against the
   exact 2026 CAD export;
3. update only latitude/longitude from parcel geometry—never valuation,
   classification, or other GIS attributes; and
4. fail closed if join coverage or source provenance is insufficient.

The TxGIO service is a **candidate for that validation**, not an approved
fallback. The directly published BCAD 2025 shapefile is the preferable
coordinate source if a separately approved enrichment is needed, because BCAD
is the source authority; but it requires the same measured join/coverage gate.
Until then, wait for BCAD to publish a link explicitly labeled "2026 Certified
Shapefiles Download" for the ordinary annual refresh.
