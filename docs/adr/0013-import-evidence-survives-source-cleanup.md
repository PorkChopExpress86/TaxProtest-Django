---
status: accepted
---

# Import evidence survives source cleanup

Both counties retain a durable import audit after source cleanup: source
identities and content digests, source years, validation results, outcome counts,
active dataset identities before and after the operation, warnings, and any
coverage-exception authorization. This preserves an explanation of publication
or rejection even when source working files are removed, without making raw-file
retention a prerequisite for keeping the audit.

Exact current published sources and unresolved blocked candidates are retained.
Superseded or rejected sources are retained for 90 days after supersession or
rejection, then may be removed while their audit records remain. Extracted
working files need not be retained if the exact retained sources support replay.

Audit records have no automatic expiry. Operators review both counties' import,
publication, exception, and writer-recovery evidence in the shared Django admin
import area, linked to the corresponding operation.

Implementation of the complete audit contract is pending; existing batch and
snapshot metadata do not establish it.
