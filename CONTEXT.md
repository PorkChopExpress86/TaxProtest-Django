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

**Harris import plan**:
The explicit selection of property, building, and GIS stages for one Harris
refresh, including the completeness expectations appropriate to that selection.
_Avoid_: scope string, flag combination

**HCAD source catalog**:
The authoritative identity and acquisition facts for HCAD source archives used
by build-time and runtime Harris imports.
_Avoid_: per-caller archive manifest, hard-coded source alias

## Brazos annual refresh

**Annual refresh**:
The complete current-year Brazos property snapshot: a year-matched certified
CAD rebuild followed by GIS enrichment, available only after both persistence
stages commit.
_Avoid_: CAD load, GIS load, partial refresh

**Coordinate enrichment**:
A non-annual update that adds only parcel coordinates to an existing CAD year
from an earlier BCAD certified GIS release, retaining the actual source year.
It may advance readiness only for an active Partial property import with older
coordinate provenance; it never reclassifies an import, substitutes for an
Annual refresh, or supplies tax impact.
_Avoid_: GIS refresh, annual refresh, current-year snapshot

**Coordinate enrichment outcome**:
The observed result of one Coordinate enrichment request: analyzed, applied,
no-op, or rejected with its measured evidence.
_Avoid_: Annual refresh status, GIS stage metric, silent dry run

**Coordinate enrichment audit**:
A durable county-owned record of one completed Coordinate enrichment analysis,
including safe coverage evidence, outcome, and any resulting update count.
_Avoid_: source archive, coordinate cache, command log

## County ETL parity

**Outcome readiness**:
The source-backed eligibility of a county property for a specific shared
tax-protest outcome. Qualified data may support search, comparables, or equity
reports while tax impact remains unavailable with explicit missing-capability
reasons; an incomplete dollar calculation is never complete tax impact.
_Avoid_: import success, all-or-nothing readiness

**Qualified replacement**:
A replacement property dataset that has passed the source and outcome-readiness
checks required for publication. Until it qualifies, the previous qualified
dataset remains available to shared reads.
_Avoid_: partially committed replacement, latest loaded rows

**Validated import preview**:
Full source validation of the exact retained files intended for application,
without publishing database changes. Database publication remains untested.
_Avoid_: source discovery, successful apply, publication guarantee

**Coverage exception**:
An explicitly reviewed authorization to publish an otherwise qualified property
replacement despite an unexplained material drop in outcome-ready coverage.
_Avoid_: successful validation, silent override

**Recovery import**:
An explicitly requested reimport of retained sources to restore a prior dataset,
subject to the ordinary source, readiness, coverage, and publication rules.
_Avoid_: snapshot toggle, validation bypass, one-click rollback

**Source-aware ETL parity**:
A county's equivalent safe delivery of a shared tax-protest outcome using only
verified source semantics, with unsupported data reported explicitly.
_Avoid_: schema parity, field parity, feature parity

**Brazos property import**:
The county-owned operation that prepares and classifies a requested Brazos
property snapshot without changing the annual-refresh source contract. Its GIS
source is year-matched; older GIS belongs only to Coordinate enrichment.
_Avoid_: ETL command, shared county importer, CAD loader

**Preflight-qualified CAD stage**:
A certified CAD source with the complete expected PACS file set, matching
source year, and verified layout and key integrity. Only this source may
publish a detailed Brazos property snapshot.
_Avoid_: parseable archive, partial CAD archive, best-effort load

**Partial property import**:
A preflight-qualified Brazos CAD snapshot published without year-matched
certified GIS, with its missing capabilities explicit. It becomes the active
snapshot for outcomes it is ready for; it is not an Annual refresh or failure.
_Avoid_: annual refresh, failed CAD load, current GIS snapshot

**Brazos property import outcome**:
The classification of a property import: completed after CAD and year-matched
GIS commit, partial after qualified CAD commits without that GIS, or failed
when no active snapshot changes. Cleanup warnings do not change the outcome.
_Avoid_: Annual refresh status, CAD stage metric, cleanup failure

**Brazos active snapshot**:
The one detailed Brazos property snapshot durably published by a completed or
partial preflight-qualified import; every shared read uses it and never falls
back to older detailed facts. Only that import's publication transaction may
supersede it; its outcome and source-year provenance remain immutable for audit.
_Avoid_: raw archive history, latest row, all-year read model, manual toggle

**Brazos readiness**:
The measured, source-backed eligibility of an active Brazos property record,
using its snapshot’s source facts, for a specific shared tax-protest outcome:
search-ready needs identity plus owner or address; comparable-ready adds
coordinates and sufficient property facts; report-ready adds assessed value
and living area; tax-impact-ready adds matching-year exemptions and rates.
_Avoid_: data ready, residential flag, annual refresh

**Brazos readiness projection**:
The immutable county-owned read result for an active snapshot: per-property
capabilities and reasons, coordinate provenance, and the fixed history view.
Shared search, subject, comparable, report, and tax reads consume it rather
than choosing years or eligibility independently.
_Avoid_: view-specific year selection, duplicated readiness query

**Comparable-ready**:
An active Brazos property with provenance-bearing coordinates and either
positive living area plus two independent comparison facts, or positive land
area for the land-only mode. Both subject and candidate qualify in the same
mode.
_Avoid_: location-only record, similarity score cutoff, residential flag

**Report-ready**:
An active comparable-ready Brazos property with positive assessed value and
living area plus at least three comparable-ready candidates with those values,
enough for actionable equity evidence.
_Avoid_: report template render, one-comparable estimate, tax impact

**Tax-impact-ready**:
An active report-ready Brazos property with matching-year jurisdiction and
exemption rows plus an adopted rate for every applicable unit. Its tax impact
never borrows another year or presents a partial dollar total.
_Avoid_: newest costable year, partial tax total, equity result

**Brazos historical coverage**:
The durable county-owned record of assessment-history years that independently
pass or fail entity-information preflight, with availability and a safe failure
category per source year. An unavailable older year does not prevent qualified
history or the active detailed snapshot.
_Avoid_: all-or-nothing history batch, active property snapshot

**Brazos history view**:
A fixed five-year window ending with the active snapshot year, where every year
is shown as available, unavailable with its safe coverage reason, or not yet
assessed. It never silently substitutes a shorter run of latest records.
_Avoid_: latest five rows, collapsed history gap
