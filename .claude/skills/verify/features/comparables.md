# Comparables

From a search result, the user opens "Similar" to see the subject property's details and a
ranked table of comparable properties, with match scores, distances and score breakdowns. They
can widen or narrow the search with radius, minimum score and result-count settings.

## Sub-features

- `comps-ranking`: comparables are sorted by match score, best first, with ties broken by
  account key.
- `comps-exclusion`: candidates that are too small, out of radius, below the minimum score, or
  ineligible never appear.
- `comps-settings`: "Search Radius", "Min Match Score" and "Max Results" with "Update Search"
  re-run the query.
- `comps-score-details`: each row has an expandable "Score Details" breakdown.
- `comps-not-found`: an unknown key returns 404.

## How to get to it (user POV)

- Click "Similar" on a search result row: `/similar/<account>/` for Harris,
  `/brazos/similar/<prop_id>/` for Brazos.
- On the page, "Back to Search" returns to the county search, and "⚖ Protest Analysis" opens
  the [protest report](./protest-report.md).

## Driving it with Playwright MCP

Preconditions:

- `verify.sh doctor` exits 0.
- Arrive from a Harris search for `Golden` by clicking the `HT0000000001` row's "Similar" link.

- **Subject.** The page title is "Similar Properties – 001 Golden St". Heading "Finding
  Properties Similar To:" is followed by heading "001 Golden St".
- **Ranking.** Under heading "Similar Properties Found", the paragraph reads "5 matching
  properties sorted by match score (best match first)". After the "YOUR PROPERTY" row, the rows
  are in this order:
  - `97.2 Best match` (Owner HC0000000001)
  - `81.1 Highly similar` (HC0000000003)
  - `81.1 Highly similar` (HC0000000004)
  - `79.4 Highly similar` (HC0000000005)
  - `54.6 Good match` (HC0000000002)
- **Exclusion.** No row for `HC0000000006`, `HX0000000001`, `HX0000000002`, `HX0000000003` or
  `HX0000000004`.
- **Settings.** `browser_select_option` on `role=combobox[name="Min Match Score"]` with `70+`,
  then `browser_click` with target `role=button[name="Update Search"]`. The URL carries
  `min_score=70` and 4 comparables remain; `HC0000000002` (54.6) is gone.
- **Score details.** `browser_click` on the first row's "Score Details". The breakdown shows
  per-factor scores such as "Living Area" and "Distance".
- **Brazos.** From a Brazos search for `Owner`, click the `BT0000000001` row's "Similar". The
  heading shows "4 matching properties sorted…", and the first comparable is `BC0000000001`.
- **Not found.** `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18020/similar/NOPE/`
  returns `404`.

## Gotchas

- The 81.1 tie between `HC0000000003` and `HC0000000004` is deliberate. Their order shows the
  account-key tie-break, so a swapped order is a regression.
- The "Borderline – depends on other factors" verdict on the subject card comes from the seeded
  values; it is not an error state.
- The protest page applies its own default minimum score of 70, so it lists fewer comparables
  than this page does at its default of 30+.
