# Brazos coordinate enrichment is not an annual refresh

When BCAD has published a certified CAD archive but not a year-matched certified GIS archive, the application may use an earlier BCAD certified GIS release only through a separate coordinate-only enrichment that measures normalized `PROP_ID` coverage before an explicitly thresholded write and records the actual GIS source year. This preserves `refresh_brazos_annual` as the sole path that publishes a year-matched CAD-and-GIS snapshot, rather than presenting stale parcel geometry as current property data.
