# TaxProtest-Django verification map

This is the maintained list of user-facing features and how to prove each one on a `verify.sh`
instance. Read this index, then follow the matching feature file as the recipe.

## Baseline preconditions

- An instance launched by this run with `.claude/skills/verify/scripts/verify.sh up`, serving
  `http://127.0.0.1:18020` (or your `VERIFY_PORT`), from a fresh database with the seed in
  `../SKILL.md`.
- `verify.sh doctor` exits 0.
- Never drive `:8020`, the user's main stack with real data.

## Driving conventions

- Every page exists once per county. Harris is at `/` and Brazos at `/brazos/`, with the same
  templates. A change to a shared view or template is proven in **both** counties. A county
  adapter change is proven in that county.
- Target elements with Playwright role selectors on accessible names
  (`role=button[name="Search Properties"]`), not snapshot refs or CSS classes.
- Start each recipe from the county's search page, the way a user arrives. Use a typed URL only
  where the recipe says so, for downloads and status codes.
- The data is read-only: no recipe writes data, so no recipe needs to restore any.

## Proof and skip reporting

- Save each proof in a `verify.sh evidence <feature>` directory: an accessibility snapshot and a
  full-page screenshot after each step, plus the landed URL.
- Save downloads (CSV, PDF) with their response headers, and assert the content type and the
  rows.
- Show both sides: the rows that should appear, and the seeded rows that must not.
- Record which county and entry point each artifact came from. If you didn't drive a county,
  don't report it as verified.
- If a journey can't reach its end state because the seed lacks a precondition, report it as
  failed with the reason, and add the reason to that feature's Gotchas. Don't weaken the journey
  to make it pass.

## Feature entry contract

Each feature file has an H1 title and a one-paragraph summary, then exactly these H2 sections in
order: `Sub-features`, `How to get to it (user POV)`, `Driving it with Playwright MCP`,
`Gotchas`.

## Features

- [Property search](./property-search.md): county search form, results table, sorting,
  eligibility filtering.
- [Search export](./search-export.md): "Export All Results to CSV" from a search, and the
  minimum-criteria guard.
- [Comparables](./comparables.md): the similar-properties page, ranking, and search settings.
- [Protest report](./protest-report.md): the protest analysis page and its CSV/PDF downloads.
