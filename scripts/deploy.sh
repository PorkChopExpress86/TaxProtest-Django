#!/usr/bin/env bash
#
# Smart deployment script for TaxProtest-Django.
#
# Runs ON the production server (invoked over SSH by .github/workflows/deploy.yml).
# Inspects which files changed between the deployed commit and origin/main, then
# does the smallest rebuild that can safely pick those changes up:
#
#   full     core infrastructure changed  -> docker compose up -d --build
#   partial  only ETL code changed        -> docker compose up -d --build $ETL_SERVICES
#   skip     only docs/non-runtime files  -> git pull, no container restart
#
# Anything that matches none of the patterns falls through to a full rebuild:
# an unrecognised file is assumed to be runtime code.
#
# Environment overrides:
#   BRANCH        branch to deploy            (default: main)
#   REMOTE        git remote                  (default: origin)
#   ETL_SERVICES  services for partial rebuild(default: "worker beat")
#   FORCE_FULL    set to 1 to force a full rebuild
#   DRY_RUN       set to 1 to print the plan without changing anything

set -euo pipefail

BRANCH="${BRANCH:-main}"
REMOTE="${REMOTE:-origin}"
ETL_SERVICES="${ETL_SERVICES:-worker beat}"
FORCE_FULL="${FORCE_FULL:-0}"
DRY_RUN="${DRY_RUN:-0}"

# Resolve the project root from this script's location so the script works
# regardless of the caller's working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

log()  { printf '[deploy] %s\n' "$*"; }
fail() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Change classification patterns (POSIX ERE, matched against repo-relative paths)
# ---------------------------------------------------------------------------

# Core infrastructure: container topology, image definition, dependencies,
# entrypoint, database schema and raw DDL. These require a full rebuild.
INFRA_PATTERN='^(docker-compose([.-][^/]*)?\.ya?ml|Dockerfile[^/]*|\.dockerignore|requirements[^/]*\.txt|pyproject\.toml|Makefile|setup\.sh)$|^scripts/entrypoint\.sh$|^data/migrations/|^taxprotest/(settings|celery|wsgi|asgi)\.py$|\.sql$|^\.github/workflows/'

# ETL surface: extract/transform/load code and the management commands that
# drive it. Only the ETL-executing services need restarting for these.
ETL_PATTERN='^data/(etl|etl_pipeline|residential|brazos_layouts|tasks_new)([./]|$)|^data/management/|^etl/|^ingest\.py$|^parsers/'

# Documentation and other non-runtime files: no restart needed at all.
# Includes the host-side deploy tooling itself — deploy.sh and cleanup_data.sh
# run on the server, never inside a container, so rebuilding for them is pure
# waste. scripts/entrypoint.sh is deliberately NOT here; it is baked into the
# image and is classified as infrastructure above.
DOCS_PATTERN='\.(md|rst|txt|png|jpe?g|gif|svg|pdf)$|^docs/|^LICENSE$|^\.github/(ISSUE_TEMPLATE|instructions|prompts)/|^\.github/[^/]*\.md$|^\.gitignore$|^\.pre-commit-config\.yaml$|^scripts/(deploy|cleanup_data)\.sh$'
# Keep requirements*.txt out of the docs bucket even though it ends in .txt.
DOCS_EXCLUDE_PATTERN='^requirements[^/]*\.txt$'

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

git rev-parse --git-dir >/dev/null 2>&1 || fail "not a git repository: $PROJECT_ROOT"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif docker-compose version >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    fail "neither 'docker compose' nor 'docker-compose' is available"
fi

docker info >/dev/null 2>&1 \
    || fail "cannot talk to the Docker daemon as $(id -un) — see DEPLOYMENT.md (non-root Docker access)"

# ---------------------------------------------------------------------------
# Fetch and diff
# ---------------------------------------------------------------------------

log "project root: $PROJECT_ROOT"
log "fetching $REMOTE/$BRANCH..."
git fetch "$REMOTE" "$BRANCH"

OLD_REV="$(git rev-parse HEAD)"
NEW_REV="$(git rev-parse "$REMOTE/$BRANCH")"

log "deployed:  $OLD_REV"
log "incoming:  $NEW_REV"

if [[ "$OLD_REV" == "$NEW_REV" ]]; then
    log "already up to date — nothing to deploy."
    exit 0
fi

CHANGED_FILES="$(git diff --name-only HEAD "$REMOTE/$BRANCH")"

if [[ -z "$CHANGED_FILES" ]]; then
    log "no file differences (commit metadata only) — fast-forwarding without rebuild."
    git pull --ff-only "$REMOTE" "$BRANCH"
    exit 0
fi

log "changed files:"
printf '%s\n' "$CHANGED_FILES" | sed 's/^/  /'

# ---------------------------------------------------------------------------
# Classify
# ---------------------------------------------------------------------------

infra_hits="$(printf '%s\n' "$CHANGED_FILES" | grep -E "$INFRA_PATTERN" || true)"
etl_hits="$(printf '%s\n' "$CHANGED_FILES" | grep -E "$ETL_PATTERN"   || true)"
docs_hits="$(printf '%s\n' "$CHANGED_FILES" | grep -E "$DOCS_PATTERN" | grep -Ev "$DOCS_EXCLUDE_PATTERN" || true)"

# Files that are neither infra, ETL, nor docs (views, templates, static, ...).
other_hits="$(printf '%s\n' "$CHANGED_FILES" \
    | grep -Ev "$INFRA_PATTERN" \
    | grep -Ev "$ETL_PATTERN" \
    | { grep -Ev "$DOCS_PATTERN" || true; } \
    | grep -v '^$' || true)"

if [[ "$FORCE_FULL" == "1" ]]; then
    MODE="full";    REASON="FORCE_FULL=1"
elif [[ -n "$infra_hits" ]]; then
    MODE="full";    REASON="core infrastructure changed"
elif [[ -n "$other_hits" ]]; then
    MODE="full";    REASON="application code outside the ETL surface changed"
elif [[ -n "$etl_hits" ]]; then
    MODE="partial"; REASON="changes isolated to the ETL surface"
elif [[ -n "$docs_hits" ]]; then
    MODE="skip";    REASON="documentation / non-runtime files only"
else
    MODE="full";    REASON="unclassified changes — rebuilding to be safe"
fi

log "decision: $MODE ($REASON)"

if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN=1 — stopping before any changes."
    exit 0
fi

# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

log "pulling $REMOTE/$BRANCH..."
git pull --ff-only "$REMOTE" "$BRANCH"

case "$MODE" in
    full)
        log "full rebuild: ${COMPOSE[*]} up -d --build"
        "${COMPOSE[@]}" up -d --build
        ;;
    partial)
        # shellcheck disable=SC2086 # ETL_SERVICES is an intentional word list.
        log "partial rebuild: ${COMPOSE[*]} up -d --build $ETL_SERVICES"
        # shellcheck disable=SC2086
        "${COMPOSE[@]}" up -d --build $ETL_SERVICES
        ;;
    skip)
        log "no container restart required."
        ;;
esac

# ---------------------------------------------------------------------------
# Cleanup and report
# ---------------------------------------------------------------------------

if [[ "$MODE" != "skip" ]]; then
    log "pruning dangling images..."
    docker image prune -f
fi

log "deployed $(git rev-parse --short HEAD) — $MODE rebuild complete."
"${COMPOSE[@]}" ps
