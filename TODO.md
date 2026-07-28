# TODO

Running list of outstanding work, kept in the repo so any session (or person) picks up
the same context. Update it in the same commit as the work it describes.

**Conventions**
- `[ ]` open · `[x]` done · `[~]` in progress or partially done
- Absolute dates only — "next week" means nothing to a session six months from now
- Each item says *why* it is open and *what unblocks it*, not just a title
- Delete items once done and merged; this is a worklist, not a changelog (git has that)

Last reviewed: 2026-07-28

---

## Blocking — do before merging `feat/brazos-county-support`

- [x] **Open the PR for `feat/brazos-county-support`**
  Opened 2026-07-28: <https://github.com/PorkChopExpress86/TaxProtest-Django/pull/2>

- [ ] **Register the five GitHub Actions secrets before that PR merges**
  `SERVER_HOST`, `SERVER_USER`, `SSH_PRIVATE_KEY`, `SERVER_PORT`, `PROJECT_PATH`.
  Merging fires `.github/workflows/deploy.yml` for the first time. Without the secrets the
  run fails at the SSH step — harmless, but nothing deploys. Setup steps: `DEPLOYMENT.md`.
  ```bash
  gh secret set SSH_PRIVATE_KEY < ~/.ssh/taxprotest_deploy
  ```

- [ ] **Prepare the server per `DEPLOYMENT.md`**
  Deploy user in the `docker` group, repo cloned at `PROJECT_PATH`, `.env` present.
  Verify the change classifier without touching containers:
  ```bash
  cd "$PROJECT_PATH" && DRY_RUN=1 ./scripts/deploy.sh
  ```
  Note: this PR classifies as a **full rebuild** (`requirements.txt`, `docker-compose*.yml`
  and migrations `0016`–`0018` all match the infra tier), so expect
  `docker compose up -d --build`. The three migrations are additive — new tables and
  nullable columns — so existing HCAD data is untouched.

---

## Brazos data gaps

- [ ] **2021 parcel coordinates are missing** (`0 / 143,391` accounts located)
  The 2021 BCAD shapefile has no usable `PROP_ID` column, so `load_brazos_gis` has nothing
  to join on. 2024 and 2025 are fine (~94.5% of `prop_type_cd='R'`; mineral, personal
  property and mobile homes have no parcel boundary and stay NULL by design).
  Next step: re-inspect the 2021 archive's layers for an alternate id column (geo id?), or
  accept 2021 as history-only — similarity search needs coordinates, so 2021 can't be a
  comparables source either way.

  Current state:

  | tax_year | accounts | with coords |
  |---|---|---|
  | 2021 | 143,391 | 0 |
  | 2024 | 149,375 | 73,962 |
  | 2025 | 149,225 | 74,792 |

- [ ] **2016–2020 exports are not loaded**
  They predate the layout revision that moved `sic_code`, so the byte offsets in
  `data/brazos_layouts.py` are likely wrong for those years. Loading them blind would
  silently write garbage into the wrong columns.
  Next step: dump one record from a 2020 export, diff the field boundaries against
  `LAYOUTS["APPRAISAL_INFO"]`, and add a second layout variant selected by export year if
  they differ. Do **not** just widen `optional=True` — that hides misalignment.

- [ ] **Tax impact is unavailable for Brazos**
  `data/tax_impact.py` needs `TaxUnitRate` + `PropertyJurisdictionExemption` rows, which are
  populated from Harris TSVs only. Brazos protest reports degrade to
  `completeness="missing"` with an explanatory note (intended behaviour, covered by
  `test_tax_impact_degrades_without_rate_data`), but the panel is empty.
  Next step: find Brazos adopted-rate and exemption sources. Per-property taxing units are
  already loaded in `PropertyAccount.entities` (100% coverage), so only the rate table and
  exemption amounts are missing.

---

## Known caveats (decide whether to act)

- [ ] **Brazos similarity scores are not comparable to Harris ones**
  Brazos publishes no bedrooms, bathrooms or condition, so `_score_from_components`
  renormalises over fewer factors and applies a different completeness penalty. *Rankings
  within a county are sound* — every candidate in one search shares the same gaps — but an
  absolute score means something different in each county.
  Options: surface a per-county score band in the UI, normalise the penalty across counties,
  or leave it and document it more loudly. Currently only noted in `CLAUDE.md`.

- [ ] **Pre-existing lint error in `data/tax_impact.py:4`**
  `from decimal import Decimal, ROUND_HALF_UP` — ruff wants `ROUND_HALF_UP, Decimal`.
  Predates the Brazos work; left alone deliberately to keep that diff focused. One-line fix
  whenever someone is touching the file anyway.

---

## Housekeeping

- [ ] **`docs/` has accumulated overlapping similarity write-ups**
  `SIMILARITY_ALGORITHM.md`, `SIMILARITY_SCORING.md`, `SIMILARITY_QUICKREF.md`,
  `SIMILARITY_FIX.md`, `SIMILARITY_INVESTIGATION_JAN2025.md`, `SCORING_UPDATE_OCT2025.md`.
  Several describe superseded behaviour and none mention the county-neutral rewrite. Only
  `docs/SIMILARITY_SCORING.md` is referenced from `CLAUDE.md`.
  Next step: fold the still-true parts into `SIMILARITY_SCORING.md` and delete the rest.

- [ ] **Staging disk check after any ETL run**
  `load_brazos_cad` deletes its own staging on success (~1.6 GB/year), but interrupted runs
  leave it behind.
  ```bash
  ./scripts/cleanup_data.sh --dry-run   # always check first
  ```

---

## Recently completed

<!-- Keep the last few so a cold session knows what just changed. Prune below ~10. -->

- [x] 2026-07-28 — Multi-county web layer: county-aware search, comparables, protest
      analysis, CSV/PDF export (`fda2366`)
- [x] 2026-07-28 — Brazos CAD pipeline: 5 PACS models, COPY-based loader, GIS loader,
      fixed-width layouts, cleanup script, esearch verification harness (`580d92d`)
- [x] 2026-07-28 — Change-aware CD pipeline: `deploy.yml`, `scripts/deploy.sh`,
      `DEPLOYMENT.md` (`3b083af`)
- [x] 2026-07-28 — Verified Brazos ETL against esearch.brazoscad.org: 60 properties,
      17 fields each, 0 unexplained mismatches
