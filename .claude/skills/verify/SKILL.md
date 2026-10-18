---
name: verify
description: Launch a throwaway, seeded instance of the TaxProtest-Django web app (Harris and Brazos property search, comparables, protest report, CSV/PDF exports) and drive it in a browser or over HTTP to prove a change works the way a user sees it. Use after changing views, templates, adapters, similarity, exports or anything a page renders, or when asked to verify, demo or screenshot the app.
---

# Verify TaxProtest-Django

The user surface is the Django web site: one shared set of pages per county (Harris at `/`,
Brazos at `/brazos/`), rendered from `templates/counties/`. There is no CLI surface for users;
management commands are operator tools and stay out of this skill.

Everything runs through one helper, from the checkout root, in Bash (Git Bash on Windows):

```bash
.claude/skills/verify/scripts/verify.sh up        # migrate, seed, serve; prints READY <url>
.claude/skills/verify/scripts/verify.sh up --build   # same, rebuilding the image first
.claude/skills/verify/scripts/verify.sh doctor    # read-only health check of that instance
.claude/skills/verify/scripts/verify.sh evidence <name>   # creates and prints .scratch/verify/<UTC>-<name>/
.claude/skills/verify/scripts/verify.sh logs [N]  # last N lines of seed + runserver output
.claude/skills/verify/scripts/verify.sh down      # removes the instance and its database
```

From PowerShell, prefix with `bash`.

## Isolation: never drive the main stack

The user's long-running stack (`taxprotest-web` on port 8020, `taxprotest-postgres`) holds real
imported county data. Never point a browser at `:8020`, never `docker compose up` from the
repo's own compose file, never stop `taxprotest-*` containers.

`verify.sh` runs a separate Compose project named `tpverify-<checkout-dir>`, with
`compose.verify.yml` layered on `docker-compose.yml`. It has its own postgres container and volume,
no published postgres port, fixed throwaway credentials that override any `.env`, and Django
`runserver` on `127.0.0.1:${VERIFY_PORT:-18020}` serving this checkout's source.

- Two checkouts (worktrees) can each run one instance at the same time. Give the second one
  another port, e.g. `VERIFY_PORT=18021`, and pass the same `VERIFY_PORT` to every
  `verify.sh` command for it.
- One checkout has one instance. If `doctor` already passes when you start, another agent may
  be driving it. Use another checkout rather than double-driving, and never `down` an instance
  you did not `up`.

## Launch

```bash
.claude/skills/verify/scripts/verify.sh up
```

- `up` builds the `tpverify-<checkout>-taxprotest-dev` image only if it is missing. Pass
  `--build` after changing `requirements.txt` or the `Dockerfile`; source code is
  bind-mounted and needs no rebuild.
- `up` is slow on Windows, because Python reads every file over the Docker Desktop bind mount.
  Measured from an empty database with the image already built: 352 s (about 1 minute of
  imports, 1½ of migrations, 2 of seeding and reloader start). The first build in a checkout
  adds 3–4 minutes. Always run `up` in the background; it waits up to 15 minutes.
- The container runs `migrate`, then `scripts/seed.py`, then `runserver`. Seeding happens only
  on an empty database.
- **Ready** means `verify.sh up` printed `READY http://127.0.0.1:18020`. The script waits until
  `/similar/HT0000000001/` returns 200. That page needs migrations applied, seed rows present
  and the server answering, so it is a stronger signal than `/healthz/`.
- If it exits early, it prints the container log tail. A failed migration or seed error is a
  real finding about the checkout: report it, don't work around it.
- Code changes are picked up live: `/app` is a bind mount of the checkout and `runserver`
  autoreloads. Rerun `up` only after changing dependencies, the Dockerfile, migrations you
  need re-applied from scratch, or the seed.

### Seed data

`scripts/seed.py` calls the similarity golden-test builders in
`counties/common/tests/similarity_scenarios.py`. Every row is synthetic: owners `Owner <key>`,
street `Golden St`.

| County | Subject | Rows | What is deliberately excluded |
|---|---|---|---|
| Harris | `HT0000000001` at `001 Golden St`, ZIP 77040 | 11 `PropertyRecord`s (`HT…`, `HC0000000001`–`06`, `HX0000000001`–`04`) | `HX0000000002` (not data-ready) and `HX0000000003` (not residential) never appear in search; `HX0000000001` (too small), `HX0000000004` (outside radius) and `HC0000000006` (score too low) never appear as comparables |
| Brazos | `BT0000000001` | `BT…`, `BC…`, `BX…` props in one completed 2025 snapshot | the scenario's ineligible and out-of-radius props |

The seed has **no** assessment history, tax-unit rates, exemptions or recorded source year.
Pages that depend on those show their "unavailable" notices. That is the expected state, not a
bug. A change that needs those rows needs a seed change, which goes in `scripts/seed.py`.

## Doctor

Run this first, and again whenever anything looks off:

```bash
.claude/skills/verify/scripts/verify.sh doctor
```

It checks that this project's `taxprotest-dev` container is running, that `VERIFY_PORT` is
published by that container (not someone else's server), that `/healthz/` (database) and `/`
return 200, and that the seeded subject resolves. It also prints the `/app` mount source and the
checkout HEAD, so you can confirm the instance serves the code you think it does. Exit code 0
means worth driving.

## Drive

**Browser** (the user path): use the Playwright MCP tools (`browser_navigate`, `browser_type`,
`browser_click`, `browser_snapshot`, `browser_take_screenshot`). The in-app browser pane
(`mcp__Claude_Browser__*`) works too for reading and clicking.

- Target elements with Playwright role selectors built from the accessible names this repo
  renders, e.g. `role=textbox[name="Street Name"]`, `role=button[name="Search Properties"]`,
  `role=link[name="⚖ Protest Analysis"]`, `role=combobox[name="Min Match Score"]`.
- Snapshot refs (`e46`) go stale after the first paint, because Tailwind loads from
  `cdn.tailwindcss.com` and restyles the page. A fill against a ref from the first snapshot
  can time out. Role selectors don't have this problem.
- Tailwind comes from that CDN, so the browser needs internet access. Without it the pages still
  work but look unstyled.
- Playwright may only write files under this checkout. Save evidence into the `verify.sh evidence`
  directory with a relative path such as `.scratch/verify/<dir>/search.png`.

**HTTP** (exports, PDFs, status codes): `curl -s -D - http://127.0.0.1:18020/<path>`. Use it
for files a user downloads and for asserting response codes. It does not replace driving the
page for anything a user sees.

The feature map in [`features/README.md`](features/README.md) has one recipe per user-facing
feature. Read the matching file before driving.

## Evidence

```bash
dir="$(.claude/skills/verify/scripts/verify.sh evidence harris-search)"
```

Put everything for one proof in that directory, `.scratch/verify/<UTC>-<name>/`, which git
ignores. Name it after the feature and the change.

- **Exercise the real user path.** Type into the form and click its buttons, then follow the
  links on the page. Don't call views, adapters or the ORM directly, and don't build URLs the
  UI wouldn't produce, except to check a response code.
- **Capture the action and the result.** Save an accessibility snapshot
  (`browser_snapshot` with `filename`) and a full-page screenshot both before and after the
  interaction. Record the URL the page landed on, which carries the submitted query.
- **Check side effects.** Exports and PDFs are files: save the body with
  `curl -o "$dir/<name>.csv"` along with its headers (`-D "$dir/<name>.headers"`), then
  assert the content type and the rows. These pages read data and never write it, so there are
  no database side effects to check.
- **Use negative cases from the seed.** A proof that rows appear is stronger when it also shows
  the excluded rows (table above) are absent.
- **No mocks.** The instance is the real app against a real Postgres. Only the data is
  synthetic.
- Playwright also writes snapshots and console logs to `.playwright-mcp/` in the checkout.
  Treat it as harness scratch, not evidence; git ignores it.

## Cleanup

```bash
.claude/skills/verify/scripts/verify.sh down
```

`down` removes only this project's containers, network and postgres volume (`down -v` on
`tpverify-<checkout>`). It never touches `taxprotest-*` containers, and it never deletes
`.scratch/verify/`, which is where proofs survive. Close the Playwright browser
(`browser_close`) when done. Never kill containers or processes by name. The image
`tpverify-<checkout>-taxprotest-dev` is kept for faster relaunches; remove it with
`docker image rm` only if disk space matters.
