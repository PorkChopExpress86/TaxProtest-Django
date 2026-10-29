---
status: accepted
---

# Property publication reviews coverage loss

For both counties, an unexplained material loss of previously supported
outcome-ready coverage blocks automatic property publication even when source
structure passes validation. Evidence must report counts by outcome and exclusion
reason; publication requires an explicit reviewed coverage exception. This favors
detecting population loss over accepting every structurally valid replacement.

Coverage compares both total outcome-ready counts and retained readiness among
previously eligible property identities, separately for each outcome. Verified
removals and deliberately missing source stages are explicit; expected Partial
limitations cannot conceal unexpected losses within supported outcomes.

An explicitly authorized operator reviews the exact replacement, differences,
exclusion reasons, and source evidence and records a reason for an exception.
Scheduled imports cannot approve their own exceptions. An unexplained loss of
more than 1% of previously eligible identities for any supported outcome blocks
automatic publication. A claimed supported outcome with zero eligible records
always blocks publication. First imports require review because no baseline
exists. The 1% limit is an initial policy, not a measured source tolerance.

Coverage exceptions never bypass malformed sources, wrong source years,
conflicting identities, or source-integrity failures. Missing prerequisites keep
the affected outcome unavailable; approval cannot mark that outcome ready.

A source row whose field count differs from its header (an HCAD stray tab or a
line break inside a value) is not a malformed source: it cannot be read by
position, so it is excluded from the load and counted in that file's invalid
validation evidence. A file in which no row matches its header still fails.

HCAD splits a few parcels into several GIS shapes under one account. Such an
account takes the centroid of the union of its distinct shapes, independent of
file order, and its account number is listed under `merged_accounts` in the GIS
file's validation evidence, which the review report shows. Exact duplicate
shapes are skipped as before. The key appears only when a merge happened.

HCAD repeats some identities, so each canonical identity is defined by what the
source can tell apart. HCAD has no feature sequence, so the Extra Feature
identity is account, code, `bld_num`, and the row's 1-based source ordinal among
rows sharing those three, counted in file order across the selected detail
files. Two sheds on one lot are two features. Fixtures are identities only for
the room types the room counts read (`RMB`, `RMF`, `RMH`). When one of those
repeats for a building with different units, that count is unknown: it is not
set from fixtures (a conflicting full or half bath makes the bathroom total
unknown), and every row of that type for the building is counted in the fixtures
file's invalid evidence. Other fixture types are not identities, and a fixture
row with a blank type is excluded and counted invalid like a misaligned row.

Implementation has not been verified. ADR-0014 defines the approval interface.
