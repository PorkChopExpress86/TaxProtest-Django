---
status: accepted
---

# Persisted import evidence is a contract

Review, approval, publication, and recovery re-compute qualification and source
bindings and compare them with the hashed values stored earlier. Any change to
the stored shape, a key name, or even the text of an evidence string makes
in-flight candidates fail their binding checks.

Stored candidate and operation evidence, its digests, the dataset identities,
retained-source records, Candidate state and Import operation status values, and
the writer lock keys are a contract. They are never renamed, retyped, reshaped,
or reworded without an explicitly approved data migration that rebinds existing
rows, and tests pin their bytes. Shared code reads county-written evidence only
through validation validity, `property_source_year`, and retained-source years;
a typed cross-county evidence protocol is not introduced.

A change that intentionally alters persisted qualification evidence (a readiness
rule or a score change while readiness still depends on scores) is released only
when no candidate of that county is awaiting review or approved, and held
candidates are re-prepared afterwards.
