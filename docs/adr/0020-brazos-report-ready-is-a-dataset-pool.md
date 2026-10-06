---
status: accepted
---

# Brazos report-ready is a dataset pool judged by one readiness rule

Brazos report-ready used to run a comparables search (10 miles, top 50, minimum
score 30) and coverage qualification ran it for every account, so publication
cost scaled with the county and ran inside time-limited admin requests. Its
score threshold also disagreed with the dossier's own minimum score, so
report-ready never promised what the report would show.

Report-ready is now judged from source facts with no distance and no similarity
score: a comparable-ready property with positive assessed value and living area
whose active snapshot holds at least three other same-mode properties that also
qualify. This matches the glossary and the Harris report pool. A 10-mile local
pool was rejected because locality is product policy rather than source
semantics (ADR-0003) and would tie persisted coverage evidence to a dossier
constant; it can be added later as a tightening for both counties.

One Brazos readiness rule, evaluated over facts gathered in bulk, answers for a
single property and for the whole snapshot, so coverage qualification and the
web surface give the same answer, enforced by a parity test. Readiness never
calls the comparables search, never searches per record during coverage, and
keeps no memo or cache, because candidate measurement switches the database
search path and the adapter is a long-lived singleton. A report-ready property
whose dossier finds fewer than three comparables at the chosen minimum score is
a Comparable shortfall, stated in the dossier rather than hidden.

Readiness and scoring keep two different "primary improvement" rules for now;
unifying them would flip some comparable-ready answers and is a separate,
separately released change.

Any change that alters persisted qualification evidence, including this rule,
is released only when no Brazos candidate is awaiting review or approved, and
held candidates are re-prepared afterwards. A compatibility mode that keeps the
old rule for existing candidates was rejected.
