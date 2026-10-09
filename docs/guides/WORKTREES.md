# Parallel checkouts

Use this when several checkouts run project commands at once: git worktrees, or parallel
agents each working one ticket.

## Running commands

Run every dev-container command through the checkout's own wrapper:

```bash
scripts/dc pytest counties/brazos/tests -q
scripts/dc ruff check .
scripts/dc python manage.py makemigrations --check --dry-run
```

`docker-compose.yml` fixes its container names, so a second checkout cannot start its own
stack. `scripts/dc` gets around that:

- **Shared stack:** every checkout joins the main checkout's Compose project and its
  postgres container.
- **This checkout's code:** the dev container mounts the checkout the script lives in, so
  it works from any directory.
- **Settings:** it reads the main checkout's `.env`; a worktree needs no copy.
- **Own test database:** each `pytest` or `python -m pytest` run gets a database named
  after the checkout plus the process id. Concurrent runs, even in one checkout, never
  share a test database, and no stale one is reused. Other commands use the shared dev
  database.

Create a worktree from the main checkout:

```bash
git worktree add ../TaxProtest-Django-wt/<name> -b <name> <base-branch>
```

## Which tests to run

On Windows the full suite takes 13–20 minutes in Docker, because the checkout is a slow
bind mount, and longer when several agents share the machine. On CI's Linux runner it
takes about 4 minutes. CI runs on every branch push, so:

- **Implementer:** run the focused tests, then `ruff`, `black --check`, `mypy` and
  `makemigrations --check`. Push the ticket branch and read its CI run for the
  directory-wide and full-suite results, then cite the run id in the report, separately from
  the local results. Run a directory locally for failures CI cannot reproduce, such as a
  Windows-only bug.
- **Integration branch:** the merger runs the full suite once per batch of merges, or
  pushes the branch and reads the CI result.

Follow a branch's latest CI run by id (`gh run watch` with no id needs a terminal):

```bash
gh run list --workflow ci.yml --branch <branch> --limit 1 --json databaseId,headSha
gh run watch <id> --exit-status
```

The run appears a few seconds after the push, so rerun the list until `headSha` is the
commit you pushed. A newer push cancels the in-progress run (a concurrency group in
`ci.yml`), so the latest run is the one to read.

CI is the only Linux run. Behaviour that depends on the filesystem, such as
directory-listing order, can pass on Windows and fail there.

## Reviewing a branch

Review from a worktree detached at that branch, or from the branch's own worktree if it
has one (`git worktree list`). From the main checkout:

```bash
git worktree add --detach ../TaxProtest-Django-wt/<name> <branch>
```

`--detach` works when another worktree already holds `<branch>`, and `<branch>` can be a
fetched `origin/<branch>`.

The primary checkout stays on the branch its owner left it: a review that checks out
another ref there moves the owner's HEAD, and carries their uncommitted changes, off their
branch.
