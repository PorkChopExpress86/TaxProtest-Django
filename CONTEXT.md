# TaxProtest-Django

TaxProtest-Django analyzes Texas property-tax protests across county-owned ETL
paths and a shared web surface.

## Deployment

**Deployment-complete revision**:
The Git revision whose deployment plan finished successfully. A safe `SKIP`
plan can make a revision deployment-complete without a rebuild.
_Avoid_: deployed HEAD, running revision

**Target revision**:
The fetched `origin/main` revision evaluated by a deployment plan.
_Avoid_: remote HEAD, checkout revision

## Harris row translation

**Translated row**:
The database-neutral, typed result of interpreting one Harris HCAD source
record. It includes an explicit loadable, skipped, or invalid status.
_Avoid_: COPY row, transformed dictionary

**Persistence adapter**:
The COPY or ORM implementation that writes translated rows and supplies
database-owned metadata. It does not own HCAD source aliases or business rules.
_Avoid_: source parser, business-rule loader

## Brazos annual refresh

**Annual refresh**:
The complete current-year Brazos property snapshot: a year-matched certified
CAD rebuild followed by GIS enrichment, available only after both persistence
stages commit.
_Avoid_: CAD load, GIS load, partial refresh
