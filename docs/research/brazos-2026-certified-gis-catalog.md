# Brazos 2026 certified GIS catalog check

Checked 2026-08-24 (America/Chicago) against first-party Brazos Central
Appraisal District (BCAD) pages only.

## Question

Does BCAD publish a 2026 *certified* parcel/shapefile archive that can be
paired with its 2026 certified CAD export for an annual refresh?

## Finding

**No—not in BCAD's published GIS catalog as checked.** The newest certified
parcel entry is explicitly labeled **2025**, and there is no entry labeled
"2026 Certified Shapefiles Download."

| Official catalog link text | Exact archive URL | Source-year evidence |
| --- | --- | --- |
| [2025 Certified Shapefiles Download](https://brazoscad.org/wp-content/uploads/2026/05/BrazosCADParcels_20260422.zip) | `https://brazoscad.org/wp-content/uploads/2026/05/BrazosCADParcels_20260422.zip` | The [BCAD GIS catalog](https://brazoscad.org/tax-information/gis/) identifies this archive as **2025 Certified**. It is the newest certified-shapefile entry, followed by 2024 and older years. |

The archive name (`BrazosCADParcels_20260422.zip`) and the server's 2026-05-02
last-modified date show that BCAD uploaded or generated a file in 2026; they do
**not** establish that the file is a 2026 certified snapshot. The catalog's
explicit certification-year label is the authoritative evidence available for
the refresh selector.

BCAD separately publishes [Download 2026 Certified
Data](https://brazoscad.org/certified-data-downloads/) for the CAD export. That
page does not publish a corresponding 2026 certified GIS/shapefile archive.

## Recommendation

**Wait/block; do not update the GIS selector.** The selector's current
link-text-based year detection correctly yields 2025. Teaching it to infer 2026
from the ZIP filename or upload path would falsely treat a catalog entry
explicitly certified as 2025 as a 2026 source, defeating the annual refresh's
year-match guard. Recheck the BCAD GIS catalog when it publishes a link labeled
"2026 Certified Shapefiles Download"; only then should the normal selector
advance to 2026.
