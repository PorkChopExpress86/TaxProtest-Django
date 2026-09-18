---
status: accepted
---

# Coverage exceptions use an admin review workflow

Both counties use one Django admin operator review surface with a dedicated
coverage-exception permission, rather than granting exception authority to every
staff user. County-owned import operations continue to own validation and
publication; the review surface is an adapter, not a shared ETL framework.

A blocked replacement remains staged while the previous qualified dataset stays
available. An authorized operator reviews its source evidence, coverage
differences, and exclusion reasons, enters a required justification, and approves
the exact replacement. Publication rechecks source identity and the active
dataset; changed inputs invalidate approval. The audit records the approver,
time, reason, and observed publication result.

Implementation is pending. The existing Harris ETL admin page does not implement
this review contract.
