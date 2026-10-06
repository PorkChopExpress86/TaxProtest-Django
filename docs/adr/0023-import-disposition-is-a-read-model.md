---
status: accepted
---

# Import disposition is a read model over the finished operation

Candidate state, Import operation status, the Harris import status, and the
Brazos workflow state were separate vocabularies joined by matching spellings,
and every command and Celery task re-derived whether published data changed from
different county result fields, with drifting operator wording.

Candidate state and Import operation status stay two persisted vocabularies with
byte-identical values, defined once in one shared module and paired explicitly
rather than by spelling. An Import disposition is derived from the finished
Import operation and the candidate it names: published, already applied, held
for an operator, or failed, and whether the import was incomplete. It is never
stored. Command and Celery adapters classify through it and keep only the
translation of their own flags into a request.

This is a read model, not a convergence. County result types, the Harris import
status members that mirror candidate states, and county runners' writes of the
operation status remain, pinned by tests, and the serialized Celery result keys
and exit codes stay stable (ADR-0002). Each removal is its own later change.
No `choices` or check constraint is added to the persisted columns until
production rows have been inspected.

Preserved behaviour, not new policy: a held import exits 0 and its Celery task
succeeds, because a county's first import is always held for review (ADR-0010);
previews keep the status `completed`. One shared operator wording reports held
imports, and a best-effort Harris run that loaded only some sources is reported
as a held, incomplete import. Persisted evidence text is never reworded
(ADR-0024). The name "outcome" was rejected because four glossary terms already
use it.
