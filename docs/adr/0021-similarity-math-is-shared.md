---
status: accepted
---

# Similarity math is shared; pair scoring stays county-owned

Harris and Brazos carried identical copies of the pure similarity helpers, the
tuning curves, score assembly, label bands, and the nearest-properties distance
query, while an unused shared module claimed they lived once. Issue #9 kept the
county similarity engines separate because they were coupled to county models;
its own principle is to reuse what is model-agnostic, and the pure math is.

The pure math lives once in common code and both counties use it. The tuning
curves are shared named constants; a county that needs different tuning defines
its own constant and justifies the difference under ADR-0003. Each county keeps
its own factor list, weights, quality semantics, candidate pre-filters, and pair
scorer. Brazos pair scoring is pure: it reads facts loaded in bulk with a fixed
number of queries and never queries per pair. Brazos improvement facts are
never keyed by `imp_id` alone, because `imp_id` repeats across properties.

The refactor is score-neutral, proved by golden results captured first. Known
behaviour differences stay as they are and each needs its own approved change:
Brazos building-free scoring when neither property has a characteristics row,
eligibility filtering after truncation, the Harris living-area window and
half-building rule versus Brazos, and two Harris primary-building rules. Those
county differences are recorded as unclassified rather than source-justified.

A shared scoring engine driven by per-county factor sets was rejected for now:
with two counties it would move county grade and condition semantics into common
code. Revisit it when a third county arrives or a second factor-level divergence
appears. Similarity results stay dictionaries.
