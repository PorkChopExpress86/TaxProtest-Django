---
status: accepted
---

# Harris replacement preserves qualified reads

A Harris property replacement must preserve the previous qualified dataset for
shared reads until the replacement passes its required source and readiness
checks. This trades the simplicity of exposing per-dataset commits for continuity
when a later building, GIS, or readiness stage fails; county-owned persistence
remains authoritative, but its intermediate writes must not become the published
replacement prematurely.

This supersedes the portions of ADR-0002 and ADR-0007 that permit earlier dataset
commits to change shared reads after a failed replacement. It does not prescribe
the staging mechanism or change the individual persistence-call contract.

Implemented: a Harris apply loads its replacement into a candidate's staged tables,
where readiness is refreshed before anything is published unless the request opts out
(`refresh_readiness=False`); covered by
`test_apply_prepares_then_refreshes_once_without_publishing_unready_data`.
Publication exchanges the staged Property, Building and Extra Feature tables in together
(ADR-0018, ADR-0025), so the previous dataset serves shared reads until then.
`HarrisFailedReplacementTests` in `counties/harris/etl_pipeline/tests/test_import_candidate.py`
verifies this for an interrupted building stage and for a blocked replacement.
