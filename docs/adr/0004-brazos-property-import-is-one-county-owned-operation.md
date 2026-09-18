# Brazos property import is one county-owned operation

All authoritative Brazos property-snapshot requests will cross a single
county-owned operation that owns preflight, source-stage selection, outcome
classification, and cleanup policy. The existing CAD, GIS, and annual-refresh
stages retain their separate source semantics and command adapters; history and
tax-rate imports remain specialized operations rather than being forced into a
cross-county ETL abstraction.
