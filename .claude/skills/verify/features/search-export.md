# Search export

After a search, the user clicks "Export All Results to CSV" and downloads every matching row,
not just the visible page. Export refuses searches too broad to be meaningful.

## Sub-features

- `export-csv`: the export link carries the current search and returns `text/csv` with one row
  per result.
- `export-guard`: an export without meaningful criteria returns 400 with an explanation.

## How to get to it (user POV)

- Run a search (see [property search](./property-search.md)). Below the results table, click
  `Export All Results to CSV`.
- The link appears only when there are results.

## Driving it with Playwright MCP

Preconditions:

- `verify.sh doctor` exits 0.
- A Harris search for Street Name `Golden` is showing (9 results).

- **Find the link.** `browser_snapshot`. A link named "Export All Results to CSV" points at
  `/export/?...street_name=Golden...`. Record its URL in the evidence; that URL is what the
  user's click requests.
- **Download.** Fetch that exact URL:
  `curl -s -D "$dir/export.headers" -o "$dir/export.csv" "http://127.0.0.1:18020<link url>"`.
  The headers show `200` and `Content-Type: text/csv`. The CSV header row starts with
  `Account Number,Owner Name,Street Number,Street Name,Zip Code,Assessed Value`. There are 9
  data rows, with no `HX0000000002` or `HX0000000003`.
- **Brazos.** Repeat from a Brazos search for Owner Name `Owner`. The link points at
  `/brazos/export/?...` and returns `text/csv` with 9 rows.
- **Guard.** `curl -s -w '%{http_code}' http://127.0.0.1:18020/brazos/export/` returns `400`
  with body "Export requires meaningful search criteria: a 5-digit ZIP code or at least 3
  non-space characters in a text filter."

## Gotchas

- Playwright MCP does not save downloads to a path you choose. Read the link URL from the
  snapshot and fetch it with curl; that is the same request the click makes.
- The export is wired per county by `CountyProfile.supports_search_export`. If a county turns it
  off, its `export/` route disappears (404), and the link should disappear too.
