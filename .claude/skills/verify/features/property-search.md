# Property search

A user picks a county, fills in at least one field of the search form, and gets a paginated,
sortable table of matching residential properties. Each row has a "Similar" link to that
property's comparables.

## Sub-features

- `search-submit`: submitting the form shows "Search Results" and "Found N matching properties".
- `search-eligibility`: Harris shows only residential, data-ready records.
- `search-sort`: column header links re-sort the table (Owner ascending is the default).
- `search-county-switch`: the "Harris County" / "Brazos County" nav links switch the whole page.
- `search-empty`: a query that matches nothing shows "No properties found".

## How to get to it (user POV)

- Open `/` (Harris) or choose `Brazos County` in the top nav (`/brazos/`).
- Harris fields: First Name, Last Name, Address, Street Name, Zip Code.
- Brazos fields: Owner Name, Address, Zip Code.
- Click `Search Properties`.

## Driving it with Playwright MCP

Preconditions:

- `verify.sh doctor` exits 0, and the seed is untouched.

- **Harris search.** `browser_navigate` to `http://127.0.0.1:18020/`. `browser_type` with
  target `role=textbox[name="Street Name"]` and text `Golden`. `browser_click` with target
  `role=button[name="Search Properties"]`. The page lands on
  `/?sort=owner_name&dir=asc&...&street_name=Golden&zip_code=`, and heading "Search Results"
  shows "Found 9 matching properties".
- **Eligibility.** In the same snapshot, the table has cells `HC0000000001` through
  `HC0000000006`, `HT0000000001`, `HX0000000001` and `HX0000000004`. It has **no**
  `HX0000000002` (not data-ready) and **no** `HX0000000003` (not residential).
- **Sort.** `browser_click` with target `role=link[name="Owner ▲"]`. The URL flips to
  `dir=desc` and the first row becomes `HX0000000004`.
- **To comparables.** `browser_click` on the "Similar" link in the `HT0000000001` row (or the
  link with URL `/similar/HT0000000001/`). This continues in [comparables](./comparables.md).
- **Brazos search.** `browser_click` with target `role=link[name="Brazos County"]`.
  `browser_type` with target `role=textbox[name="Owner Name"]` and text `Owner`. Click
  `Search Properties`. Results show "Found 9 matching", with "Similar" links to
  `/brazos/similar/<prop_id>/`, including `BT0000000001`.
- **Empty.** On Harris, search Street Name `Nowhere`. The page shows "No properties found" and no results
  table.
- **Proof.** After each step, `browser_snapshot` with `filename: .scratch/verify/<dir>/<step>.yml`
  and `browser_take_screenshot` with `fullPage: true, filename: .scratch/verify/<dir>/<step>.png`.

## Gotchas

- A ref from the first snapshot can time out on `fill`, because the Tailwind CDN re-renders the
  page. Use role selectors.
- Every Harris seed row shows `$350,000` and `2,200` building area. Those come from
  `PropertyRecord` defaults in the seed, not a bug. Bed/bath, quality and features do differ.
- "Harris property source year: Not recorded" is expected: the seed records no source year.
- The Harris Zip Code field has `pattern="[0-9]{5}"`, so the browser blocks submitting a
  non-5-digit ZIP before any request is sent.
