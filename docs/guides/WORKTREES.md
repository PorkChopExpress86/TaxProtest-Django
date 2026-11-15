# Parallel checkouts

Use this when several checkouts run project commands at once: git worktrees, or parallel
agents each working one ticket.

## Running commands

Run every dev-container command through the checkout's own wrapper:

```bash
scripts/dc pytest counties/brazos/tests -q
scripts/dc fmt     # ruff check --fix . && black .
scripts/dc check   # ruff, black --check, mypy, makemigrations --check in one container
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
git worktree add --no-track ../TaxProtest-Django-wt/<name> -b <name> <base-branch>
```

`--no-track` matters when `<base-branch>` is a remote ref such as `origin/main`: without it the
new branch tracks `main`, and a bare `git push` aims at it. Push a new branch with
`git push -u origin <name>`.

## Which tests to run

On Windows the full suite takes 13–20 minutes in Docker, because the checkout is a slow
bind mount, and longer when several agents share the machine. On CI's Linux runner it
takes about 4 minutes. CI runs on every branch push, so:

- **Implementer:** run the focused tests, then `scripts/dc fmt` and `scripts/dc check` (`ruff`,
  `black --check`, `mypy`, and `makemigrations --check`). Push the ticket branch and read its CI run
  for the directory-wide and full-suite results, then cite the run id in the report, separately from
  the local results. Run a directory locally for failures CI cannot reproduce, such as a
  Windows-only bug.
- **Integration branch:** the merger runs the full suite once per batch of merges, or
  pushes the branch and reads the CI result. It merges each ticket with
  `git merge --no-ff -m "Merge #<ticket>: ..."`: the implementer has already merged the
  integration tip in, so a plain merge fast-forwards and the ticket loses its own reviewable
  merge commit.
- **Characterization tests:** a ticket that pins today's behaviour also runs its new tests on
  the base commit (a `--detach` worktree there, with the new test files copied in) before it
  merges, so the pins describe the code they claim to describe.

Follow a branch's latest CI run by id (`gh run watch` with no id needs a terminal):

```bash
gh run list --workflow ci.yml --branch <branch> --limit 1 --json databaseId,headSha
gh run watch <id> --exit-status
```

The run appears a few seconds after the push, so rerun the list until `headSha` is the
commit you pushed. A newer push to a branch other than `main` cancels the in-progress
run (a concurrency group in `ci.yml`), so the latest run is the one to read.

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
