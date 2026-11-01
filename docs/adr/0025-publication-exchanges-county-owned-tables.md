---
status: accepted
---

# Publication exchanges county-owned tables

Publication cut a candidate over by deleting every published row and inserting the
staged rows inside one transaction. For a full Harris dataset that took 417 s and
18.4 GB of WAL (#144), dominated by index maintenance, and left every table and index
half dead tuples until vacuum. Dropping indexes around the insert was rejected: the
dropped indexes hold an exclusive lock until commit, so live search would wait out the
whole rebuild, and the delete still writes its WAL and bloat.

Publication instead exchanges each county-owned table: in the publication transaction,
under a bounded lock wait, the published table moves into the candidate schema and the
staged table moves into `public`. A rollback leaves the previous tables published, and
the replaced tables are dropped with the candidate schema, so no previous dataset stays
selectable (ADR-0016).

A snapshot does not isolate an exchanged table: a shared response that first reads it
after the exchange commits sees the new rows beside the old rows of everything else.
Shared responses and exchanges therefore exclude each other. Each shared response holds
a shared publication lock, taken before its snapshot, for its whole response; an
exchange takes that lock exclusively with its table locks. An exchange waits a bounded
time for in-flight responses and fails without publishing when they outlast it, leaving
the candidate to apply again, and new responses wait while it holds or waits for its
locks.

A table is exchanged only when the county owns all of its rows and no table outside the
county's declared set references it. A shared table scoped by `county=`, such as
`PropertyJurisdictionExemption`, keeps row replacement for that county's rows.

An exchanged table becomes the production table, so candidate staging gives every staged
table, index, constraint, and sequence production's exact names and definitions.
Migrations keep finding them by name. Publication refuses a candidate whose staged
definitions no longer match production's, as when a migration ran after preparation,
and statistics are gathered on staged tables before the exchange.

The exchange preserves the staged rows and their ids, so dataset identities are
unchanged (ADR-0024). Publication reads the published identity from the exact relations
it makes live before the exchange takes its locks, so shared reads wait only for the
exchange itself and the end of its transaction. Publication still runs under the county writer reservation
(ADR-0011), and preparation's own staging cost is unchanged.
