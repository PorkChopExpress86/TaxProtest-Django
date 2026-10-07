---
status: accepted
---

# County registration refines the candidate port

Shared Import operation code held three county-name literals: the writer lock
keys, the warning loggers, and the source-root prefix used by retention. Most
audited operations are not candidate operations, so these facts belong to the
Import operation, not to the county candidate port that ADR-0018 defines as
staged tables plus three questions.

Each county makes one County registration when its app is ready: its unchanged
county candidate port plus the facts shared code cannot derive, namely its
writer lock key, its warning logger, and a reader for its managed source roots.
The registration declares facts and pure readers only; it never holds sources,
parsing, validation, readiness, or a runner, so it is not the cross-county ETL
framework that ADR-0018 rejects. Shared lifecycle, review, recovery, retention,
and writer code read county facts only through it and hold no county names. An
unregistered county is rejected before any Import operation is recorded.

Writer lock keys are declared, unique, and frozen (742101 for Harris, 742102 for
Brazos); they are never derived from the slug. Warning logger names and
retention's deletion reach stay as they are: retention still deletes only
attempt-owned sources under each county's configured roots. `property_source_year`
stays an optional shared evidence key read by review and recovery, with its byte
shape unchanged.

Not everything derives from the registration: county choices frozen in
migrations, installed apps, runtime path specs, URLs, navigation, and the
deployment gate's core-table list stay static until a third county exists.
Warning capture stays one named logger per county. Rejected: widening the
candidate port, a second operation port, an open facet registry, and a shared
enumerated config module.

The operator benchmark keeps a county-neutral harness in common code; its county
bodies and synthetic-data builders live in the county packages as non-test
modules, so no shared module imports county or test code.
