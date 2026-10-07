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
- **Own test database:** each `pytest` run gets a database named after the checkout plus
  the process id. Concurrent runs, even in one checkout, never share a test database, and
  no stale one is reused. Other commands use the shared dev database.

Create a worktree from the main checkout:

```bash
git worktree add ../TaxProtest-Django-wt/<name> -b <name> <base-branch>
```

## Which tests to run

On Windows the full suite takes 13–20 minutes in Docker, because the checkout is a slow
bind mount. On CI's Linux runner it takes about two minutes. CI runs on every branch
push, so:

- **Implementer:** run the focused tests, then the test directories its change touches:
  `counties/common/tests` for shared code, `counties/<slug>/tests` for a county. Then run
  `ruff`, `black --check`, `mypy` and `makemigrations --check`.
- **Integration branch:** the merger runs the full suite once per batch of merges, or
  pushes the branch and reads the CI result.

CI is also the only Linux run. Behaviour that depends on the filesystem, such as
directory-listing order, can pass on Windows and fail there.
