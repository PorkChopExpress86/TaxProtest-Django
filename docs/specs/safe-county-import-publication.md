## Problem Statement

Operators importing Harris or Brazos property data cannot currently rely on one
consistent explanation of what was validated, what users can safely use, or what
changed when an import failed. A Harris Property replacement may commit and
remove dependent buildings before a later stage fails, reducing usable coverage.
A Brazos dry run does not fully inspect its CAD source contents. Existing import
metadata does not establish a complete, durable record of source bytes,
qualification, coverage exceptions, and writer recovery.

Operators need to prepare and review replacements without disrupting the last
qualified dataset, publish only supported tax-protest outcomes, and investigate
failures and approvals through Django admin. A successful source download or
database stage must not imply that comparables, equity reports, or tax impact
are available.

## Solution

Provide a Django admin Imports area for both counties, backed by their separate
county-owned import operations. It shows the active dataset, current operation,
last result, missing capabilities, blocked replacements, recovery-required
states, and searchable audit history.

A replacement is prepared and qualified before it changes shared reads. The
previous qualified dataset remains available until publication succeeds.
Validated import preview fully validates the exact retained source files
intended for application without publishing database changes, and explicitly
states that database publication remains untested.

Coverage is measured separately for each outcome. An unexplained loss exceeding
1% of previously eligible property identities blocks automatic publication. A
claimed supported outcome with zero eligible records always blocks publication;
first imports require review. An authorized operator may approve a coverage
exception for an otherwise qualified replacement through admin after reviewing
the evidence and recording a justification. Source-integrity failures cannot be
overridden, and approval cannot invent missing capabilities.

Only one writer may act for a county at a time. Competing requests fail
immediately with the active operation identity. Uncertain writer ownership is
visible in admin and requires verified, audited recovery. Exact current and
unresolved sources remain available; superseded or rejected sources are kept
for 90 days. Audit records have no automatic expiry.

## User Stories

1. As a property user, I want the previous qualified dataset to remain available during replacement preparation, so that an import does not interrupt supported analysis.
2. As a property user, I want a failed replacement to leave published property facts and capabilities unchanged, so that a later failed stage cannot erase usable data.
3. As a property user, I want each outcome's missing capabilities explained, so that I understand whether I can search, compare, produce equity evidence, or calculate tax impact.
4. As a property user, I want incomplete tax inputs to withhold tax impact, so that I never mistake a partial dollar calculation for a complete result.
5. As a property user, I want the active source year and coordinate provenance represented accurately, so that older GIS is not presented as certified current-year GIS.
6. As a property user, I want newer qualified Brazos Partial data to support its available outcomes, so that missing GIS does not prevent every useful property read.
7. As a property user, I want shared reads to use one active Brazos detailed snapshot, so that superseded detailed facts are not silently mixed into current analysis.
8. As a property user, I want history gaps identified explicitly, so that missing older years do not look like continuous history.
9. As an operator, I want one admin Imports entry point for both counties, so that I can find status, review actions, and evidence without searching separate logs.
10. As an operator, I want to see each county's active dataset, current operation, and last result, so that I know what is published and what is still running.
11. As an operator, I want a validated import preview of the exact retained files, so that validation evidence corresponds to my intended application inputs.
12. As an operator, I want preview to state that database publication is untested, so that I understand the limits of its guarantee.
13. As an operator, I want source-integrity and source-year failures to block publication, so that structurally unsafe or mismatched data cannot replace qualified data.
14. As an operator, I want coverage totals and retained readiness by property identity for each outcome, so that newly added properties cannot conceal unexpected losses.
15. As an operator, I want verified removals, explicit Partial limitations, and unexplained exclusions distinguished, so that legitimate source changes do not hide import defects.
16. As an operator, I want unexplained losses above 1% to block automatic publication, so that a materially reduced replacement receives review.
17. As an operator, I want first imports to require review, so that an absent comparison baseline is not treated as evidence of zero loss.
18. As an operator, I want a blocked replacement retained with its evidence, so that I can review it without recreating its source inputs.
19. As an authorized reviewer, I want to inspect exact replacement evidence and enter a justification, so that a coverage exception is deliberate and attributable.
20. As an authorized reviewer, I want approval tied to the reviewed source and active dataset identities, so that it cannot authorize different inputs or an obsolete comparison.
21. As an authorized reviewer, I want changed inputs to invalidate approval, so that publication cannot proceed using stale review evidence.
22. As an administrator, I want a dedicated coverage-exception permission, so that staff access alone does not confer exception authority.
23. As an operator, I want annual Brazos refresh to remain strict about year-matched CAD and GIS, so that an annual request cannot silently become CAD-only publication.
24. As an operator, I want to request Brazos Partial publication explicitly and see its limitations before applying, so that the capability trade-off is deliberate.
25. As an operator, I want specialized history or tax-data failures to leave qualified property publication possible, so that independent capability gaps do not block every supported outcome.
26. As an operator, I want a competing same-county writer rejected with the active operation identity, so that overlapping scheduled, manual, and recovery work cannot compete.
27. As an operator, I want different counties to run independently, so that county isolation does not unnecessarily serialize unrelated work.
28. As an operator, I want a prominent recovery-required state for uncertain writer ownership, so that I know why new writes are blocked and where to investigate.
29. As an authorized recovery operator, I want to verify the previous writer cannot continue before releasing its reservation, so that recovery cannot create competing writers.
30. As an operator, I want cleanup failures reported as warnings after committed publication, so that I do not retry an already-applied import as if publication failed.
31. As an operator, I want to review audit history across both counties and open its operation details, so that I can explain publications, failures, exceptions, and recovery actions.
32. As an auditor, I want source identities, digests, years, qualification results, outcome counts, dataset transitions, and approval evidence retained, so that decisions remain explainable after source cleanup.
33. As an operator, I want current published and unresolved blocked sources retained, so that I can investigate or replay the data still in use or awaiting a decision.
34. As an operator, I want superseded and rejected sources retained for 90 days while audits remain indefinitely, so that source cleanup does not erase decision history.
35. As an operator, I want restoration to use an explicit recovery import of retained sources, so that returning to older data receives the same safety checks as a new publication.
36. As an administrator, I want existing data and provenance preserved when this workflow is introduced, so that adopting it does not reset production or invent historical evidence.

## Implementation Decisions

- Preserve county-owned ETL. Extend the authoritative Harris import operation
  and Brazos property-import operation; CLI, Celery, and admin remain adapters.
  Do not introduce a generic cross-county ETL framework. Shared operator
  presentation may consume county-provided status and evidence.
- Keep shared property pages and exports behind CountyAdapter and CountyProfile.
  Retain the identical county route set. County eligibility remains source-aware:
  do not force identical schemas or infer unsupported source semantics. Preserve
  Harris's residential and data-ready query invariant and the documented Brazos
  outcome predicates.
- Separate preparation, qualification, review, and publication. Candidate writes
  must not become published facts prematurely. Select the smallest county-owned
  staging mechanism that preserves the current persistence contracts and allows
  a blocked replacement to survive the operation for later admin review.
  This spec fixes observable safety, not a particular staging schema.
- Preserve the previous qualified dataset and its dependent usable facts after
  failed or blocked replacement, including failure after Property processing,
  during building or GIS processing, readiness calculation, or publication.
  Publication must expose a consistent qualified dataset to each shared read,
  rather than a mixture of candidate and previous detailed facts.
- Keep translated-row persistence, canonical identities, Replace/Add Missing
  semantics, duplicate handling, dependent invalidation, and COPY/ORM selection
  county-owned. Intermediate candidate commits may exist, but cannot invalidate
  the published dataset. Do not silently fall back from COPY to ORM.
- A validated import preview may acquire, extract, retain, and validate sources
  and record evidence; it cannot change published property or tax data. Validate
  the complete requested source set and retain exact content identities. Source
  discovery is distinguishable from a validated preview. Recheck retained
  content before application; substituting changed files cannot reuse validation
  evidence or approval.
- Retain actual target and source years independently where their semantics
  require it. Brazos annual refresh requires qualified year-matched certified CAD
  and GIS. GIS failure preserves the prior active dataset and cannot silently
  publish Partial data. CAD-only Partial publication is an explicit request with
  limitations shown before application; it may supersede an older more capable
  snapshot after qualification and applicable review.
- Older Brazos GIS remains separately thresholded Coordinate enrichment,
  preserving its actual source year. It cannot establish annual publication,
  reclassify an import, or supply missing tax impact. Year-matched GIS recovery
  continues to require the applicable active Partial snapshot and must recheck
  that prerequisite at publication.
- Readiness and coverage use county-owned criteria for search, comparables,
  equity reports, and tax impact. Classifying source stages as completed does
  not assert all outcomes are ready. An outcome with absent prerequisites is
  unavailable with reasons; a claimed supported outcome with no eligible
  records fails qualification. Do not reduce eligibility denominators by
  deleting incomplete records to make coverage pass.
- Report total ready counts and retained readiness among previously eligible
  canonical property identities, separately for each outcome. Record additions,
  verified removals, lost eligibility, exclusion reasons, and deliberately absent
  stages. New identities do not offset old identities losing readiness. Absence
  alone is not proof of a verified source removal.
- For each supported outcome, calculate unexplained lost-ready identities as a
  proportion of the previously eligible identity population. More than 1% blocks
  automatic publication; exactly 1% does not trigger this block. Preserve full
  count precision for the decision rather than applying display rounding.
  Explicit missing Partial capabilities are reported separately and cannot hide
  unexplained losses in outcomes the candidate supports. An absent eligible
  baseline yields unavailable comparison evidence, not a fabricated zero loss.
- First imports require explicit review. An authorized coverage reviewer uses
  the same exact-candidate evidence and justification workflow for this
  no-baseline decision. Review does not bypass source or readiness qualification.
- History and tax imports remain specialized, independently classified operations.
  Their failure does not prevent otherwise qualified property publication.
  Preserve county scoping for every shared tax-table read and write, matching-year
  requirements, fractional rates, gross base values with companion exemption
  reductions, and canonical county-specific cap interpretation. No partial tax
  dollar total or borrowed-year tax impact may be presented as complete.
- Persist operation identity, county, request intent, source identity and digests,
  source years, validation evidence, coverage baseline identity, candidate identity,
  observed publication state, and warnings. Keep source classification distinct
  from running, blocked, awaiting-review, failed, or published workflow state.
  Existing caller result contracts must continue to describe observed work
  truthfully; staged work cannot be reported as an active published replacement.
- Persist coverage approvals against the exact candidate, source identities, and
  active comparison dataset, with actor, time, and required justification.
  Approval is not proof of application. Recheck these identities, qualification,
  and permission at publication; changed inputs require fresh evidence and review.
  Duplicate or stale action submission must not publish the candidate twice.
- Coverage exception authority uses a dedicated Django permission. Apply existing
  Django authentication, authorization, and CSRF conventions to admin actions.
  Recovery must likewise be restricted to explicitly authorized operators;
  ordinary staff membership is not writer-recovery authority. Audits are
  read-only in the operator interface and are not exposed publicly.
- The admin Imports area presents both counties' active dataset identity and year,
  current operation, last result, capability reasons, review-required candidates,
  and a prominent recovery-required state. Operation detail links source evidence,
  validation and coverage differences, actions, and audit history. Status is
  durable and available across sessions; session-local task IDs alone do not
  establish writer ownership or publication.
- Reject concurrent same-county writers immediately with the owning operation
  identity. All in-scope live-data writers, including scheduled imports, manual
  commands, recovery, history/tax imports, and coordinate updates, honor county
  coordination; different counties may write independently. A staged candidate
  awaiting human review does not itself imply a running writer, and later
  publication reacquires coordination and revalidates its baseline.
- Release a reservation automatically only when its writer has definitively
  ended. An uncertain owner blocks further writers; timeout, missing heartbeat,
  or absent task result alone does not prove safe release. Admin recovery records
  its evidence and authorized actor and must establish that the prior writer
  cannot continue, including resumed execution, before permitting another writer.
- Audit import attempts and observed results, publication transitions, exceptions,
  rejection, recovery, and cleanup warnings. Preserve source identities/digests
  and years, validation evidence, outcome counts, active dataset identities before
  and after, actor/time/reason where applicable, and links to the operation.
  Audit records have no automatic expiry and remain reviewable after raw cleanup.
  Do not manufacture source hashes or approval facts for legacy imports.
- Retain exact current published sources and unresolved blocked candidates.
  Retain superseded sources for 90 days after supersession and rejected sources
  for 90 days after rejection; retain shared source files while any protected
  current or unresolved reference still needs them. Extracted working files may
  be removed when retained sources support replay. Cleanup must respect these
  references and preserve the audit independently.
- A cleanup failure after committed publication is a warning, not a retryable
  publication failure. Retrying an operation cannot inadvertently repeat a
  committed publication merely because its cleanup or result reporting failed.
- Restoration requires an explicit recovery import of retained sources through
  ordinary qualification, coverage review, coordination, and audit. Do not add a
  simple old-snapshot pointer toggle. Missing retained sources prevent promising
  a replay that cannot be performed.
- Add only the durable schema needed for candidate/evidence/review/coordination
  and publication safety. Preserve existing tables, pinned Django app labels,
  migration ownership, content types, source data, and historical identities.
  Introduce the workflow non-destructively; missing legacy evidence remains
  explicitly unrecorded. Runtime files use the established county runtime roots.

## Testing Decisions

- Prefer the highest existing behavioral seams: the authoritative Harris import
  operation and Brazos property-import operation, observing published facts
  through shared adapters/pages/exports. Use Django admin requests for review,
  permissions, recovery, status, and audit browsing. Use real PostgreSQL
  transaction tests for coordination and publication races. Do not build a
  generic importer solely to provide a single testing seam.
- A good test invokes an operator or county operation and asserts externally
  visible data, capabilities, returned state, and durable evidence. Avoid tests
  coupled to private helper order, a particular staging schema, SQL text, or
  merely mirroring implementation branches. Patch acquisition or failure points
  only to make synthetic inputs and interruptions deterministic.
- Prior art includes Harris import-request preview/apply and cleanup-warning
  tests, Property/Building/Extra Feature persistence preservation tests, Brazos
  property publication and PACS preflight tests, annual GIS-failure rollback tests,
  coordinate-enrichment transaction/audit tests, readiness tests, shared county
  route/report tests, and Harris admin access/queueing tests.
- Prove prior published facts and dependent rows remain usable when candidate
  processing fails at each meaningful stage, including after Property processing
  and during readiness or publication. Observe reads while the candidate is
  prepared and prove successful publication changes the active data consistently.
  Include a same-year replacement so a different tax year cannot mask exposure.
- For both counties, prove a validated preview inspects malformed and incomplete
  retained sources, writes no published property or tax data, and explicitly
  reports publication as untested. Change source bytes between preview and apply
  and prove stale evidence cannot authorize publication.
- Test coverage at exactly 1% and just above 1%, total-count growth that conceals
  lost prior identities, verified removals, deliberately absent Partial stages,
  zero eligible supported outcomes, no eligible baseline, and first-import review.
  Source-integrity failures remain blocking even for an authorized reviewer.
- Exercise authorized and unauthorized admin requests, required justification,
  exact-candidate approval, approval invalidated by source or active-dataset change,
  repeated action submission, and separation of approval from observed publication.
  Verify audit actor, reason, identities, and result through the review interface.
- Prove annual Brazos GIS failure cannot auto-publish CAD-only data, explicit
  Partial publication shows its limitations, no older detailed fallback occurs,
  and older coordinate enrichment keeps provenance without upgrading classification.
  Test recovery prerequisites against a concurrently changed active snapshot.
- Prove history or tax-data failure does not block qualified property outcomes,
  history gaps remain explicit, missing matching-year inputs withhold complete
  tax impact, and same account/unit identities in the other county are untouched.
- Use distinct database connections and deterministic synchronization to prove
  same-county competing writers reject immediately with owner identity, separate
  counties can proceed, failed operations release safely, uncertain owners block,
  and audited recovery cannot allow an old writer to resume publication.
- Test source retention at the 90-day boundary with a controlled clock, current
  and unresolved reference protection, shared-file references, and durable audit
  access after cleanup. Verify cleanup failure preserves committed publication
  and does not cause a repeated application on retry.
- Use synthetic county files, temporary runtime roots, and isolated test databases.
  No live imports or production data are required. Run focused tests before the
  broader affected suites and full suite; run project lint, formatting, types,
  migration checks, and diff checks through the established Docker Compose
  development service. Verify admin behavior in a browser when available and
  report any unperformed browser validation.
- Implementation is complete only when the behaviors above pass and existing
  county route, eligibility, provenance, persistence, and tax-data contracts remain
  satisfied. Documentation or task completion status alone is not validation.

## Out of Scope

- A generic cross-county ETL engine or identical county source schemas.
- New county integrations, inferred source semantics, invented legacy provenance,
  changes to similarity scoring, or weakening residential/queryable invariants.
- Automatic fallback from Brazos annual refresh to Partial publication, older
  detailed-fact substitution, borrowed-year tax impact, partial dollar totals,
  or treating older GIS as certified year-matched GIS.
- Source-integrity overrides, scheduled self-approval, timeout-only writer release,
  competing writers, or one-click historical snapshot toggles.
- A separate public operator application or email/Slack notification system.
- Production import, deployment, live-data cleanup, acceptance of an actual
  candidate, configuration/permission grants to real users, commit, push, or
  issue closure as part of writing this specification.

## Further Notes

- This specification synthesizes the confirmed design interview covering both
  counties. The recorded domain glossary and ADRs 0003–0005 and 0008–0016 are the
  policy baseline. ADR-0008 supersedes the earlier permission for failed Harris
  replacements to alter shared reads; individual persistence-call ownership stays
  intact. The new policies must not be mistaken for implemented behavior.
- The 1% loss threshold is an initial agreed policy, not an empirically measured
  source tolerance. Existing Harris completeness checks use currently stored
  residential records and cannot alone prove retained-source population coverage.
  Existing county validation remains relevant in addition to the replacement gate.
- Current Brazos property dry run omits full CAD preflight. Current Harris
  persistence commits datasets separately. Existing batch, download, and snapshot
  metadata do not establish the complete digest/audit/coordination contract.
- Staging schema, reservation mechanism, and admin implementation details may be
  selected during implementation if they satisfy the observable contracts and
  preserve county ownership. They are not licenses to expand the agreed scope.
- Source retention consumes disk space, and indefinitely retained audit evidence
  must remain distinct from temporary logs and task results. Implementation should
  expose cleanup warnings and retained-source status without promising recovery
  for files already unavailable.
