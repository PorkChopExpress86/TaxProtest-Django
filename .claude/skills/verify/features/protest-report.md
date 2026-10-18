# Protest report

From the comparables page the user opens "⚖ Protest Analysis". It shows an equity verdict
against the comparable median $/sqft, tax impact, assessment history, a $/sqft distribution
chart, and the comparable table. They can download the same evidence as CSV or PDF, or print it.

## Sub-features

- `protest-verdict`: the page shows either "Potential Equity Protest Case" or "At or Below
  Comparable Median".
- `protest-comparables`: the "Comparable Properties" table lists comparables scoring at least
  70.
- `protest-notices`: missing tax or history inputs show "unavailable" notices instead of
  numbers.
- `protest-csv`: "Download CSV" returns `text/csv`, one row per comparable, with score
  breakdowns.
- `protest-pdf`: "Download PDF" returns `application/pdf`.

## How to get to it (user POV)

- On a comparables page, click `⚖ Protest Analysis`: `/protest/<account>/` for Harris,
  `/brazos/protest/<prop_id>/` for Brazos.
- On the report: `Back to Similar Properties`, `Download CSV`, `Download PDF`, and a print
  button.

## Driving it with Playwright MCP

Preconditions:

- `verify.sh doctor` exits 0.
- On `/similar/HT0000000001/`, reached from a search.

- **Open.** `browser_click` with target `role=link[name="⚖ Protest Analysis"]`. The page lands
  on `/protest/HT0000000001/`, with h1 "Protest Analysis" and h2s "At or Below Comparable
  Median", "Tax Impact", "Comparable $/Sqft Distribution" and "Comparable Properties".
- **Verdict.** "At or Below Comparable Median" shows for the seeded subject. With identical
  assessed values, the subject's $/sqft is not above the median.
- **Notices.** The page says tax impact is unavailable until all matching-year jurisdiction,
  exemption and rate inputs are complete, and shows "Assessment history unavailable. Qualified
  property evidence remains available."
- **CSV.** In the snapshot, the "Download CSV" link URL is `/protest/HT0000000001/export/?min_score=70`.
  `curl -s -D "$dir/protest.headers" -o "$dir/protest.csv" "http://127.0.0.1:18020/protest/HT0000000001/export/?min_score=70"`.
  The response is `200 text/csv`. The header row starts
  `address,similarity_score,similarity_label`, and the 4 data rows are 001, 003, 004 and 005
  Golden St with scores 97.2, 81.1, 81.1 and 79.4.
- **PDF.** Fetch the "Download PDF" link URL (`/protest/HT0000000001/pdf/?min_score=70`) with
  `-D`/`-o "$dir/protest.pdf"`. The response is `200 application/pdf`, and the file starts with
  `%PDF`.
- **Brazos.** Repeat from `/brazos/similar/BT0000000001/`. The report is at
  `/brazos/protest/BT0000000001/`, and its PDF returns `application/pdf`.

## Gotchas

- The seed has no source year, assessment history, tax rates or exemptions. The Tax Impact
  numbers and the history table are therefore always "unavailable" here. A change to tax impact
  or history rendering can't be proven until `scripts/seed.py` seeds those inputs. Report it as
  failed for that reason; don't skip it silently.
- The CSV and PDF links carry the page's `min_score`. Fetch the URL from the snapshot rather than
  a hand-built one, or you'll prove a different query.
- The PDF is small (about 2 KB) because the seed is tiny. Check the content type and the `%PDF`
  magic bytes, not the size.
