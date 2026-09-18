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

Implementation is pending: the current Harris orchestrator writes datasets before
refreshing readiness and validating completeness, and Property replacement
invalidates dependent Building and Extra Feature rows.
