---
status: accepted
---

# The candidate lifecycle is shared; source semantics stay county-owned

Harris and Brazos each implemented the same candidate lifecycle: preparation in an
isolated schema, coverage qualification, review binding, fenced publication, audit,
and recovery. The copies drifted, and shared review and recovery code branched on
county names to reach into each county's request shape and evidence keys.

Common code owns that lifecycle, run inside an Import operation. Each county
registers one county candidate port from its app configuration, and common code
resolves ports only by county slug. A port declares the county's staged tables and
answers three questions: the facts that identify its live dataset, its outcome
populations for the published data or a candidate, and how to replay retained
sources for recovery. Fresh preparation receives the county's load from the county
runner rather than calling back into the port.

Counties still own source acquisition, parsing, validation, readiness criteria, and
the meaning of their evidence. Common code never interprets a county's sources or
decides its readiness, so this refines ADR-0014 without creating a generic
cross-county ETL framework.
