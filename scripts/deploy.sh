#!/usr/bin/env bash
# Smart deployment script for TaxProtest-Django.
#
# Runs on the remote Linux server after the GitHub Actions workflow
# SSHes in. Classifies the set of files changed on origin/main vs. HEAD
# and chooses the lightest-weight rebuild that still picks up the changes.
#
# Classification order:
#   1. FULL — any infrastructure / schema / settings change triggers a
#      full container rebuild via `docker compose up -d --build`.
#   2. PARTIAL — only ETL / parser / loader code changed; rebuild just
#      the `etl` service.
#   3. SKIP — only docs, comments, or CI config changed; `git pull` only.
#   4. Default (anything else) — FULL rebuild (safe).
#
# After every classification, `git pull --ff-only` runs and dangling
# Docker image layers are pruned.
#
# Migrations and `collectstatic` are intentionally NOT run here — they
# are handled by scripts/entrypoint.sh on container start and at Docker
# build time respectively. Keeping that responsibility in one place
# avoids double-application races.

set -euo pipefail

# --- preconditions -----------------------------------------------------------

for cmd in git docker; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: required command not found in PATH: $cmd" >&2
        exit 1
    fi
done

if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: 'docker compose' plugin not available; install Docker Compose v2+" >&2
    exit 1
fi

# --- locate the project root -------------------------------------------------

PROJECT_PATH="${PROJECT_PATH:-$(pwd)}"
if [[ ! -d "$PROJECT_PATH/.git" ]]; then
    echo "ERROR: $PROJECT_PATH is not a git working tree" >&2
    exit 1
fi
cd "$PROJECT_PATH"

echo "==> Deploying in $PROJECT_PATH"
echo "    HEAD:   $(git rev-parse --short HEAD)"
echo "    remote: $(git rev-parse --short origin/main 2>/dev/null || echo 'unknown')"

# --- classify changed files ---------------------------------------------------

git fetch --quiet origin main

if git diff --quiet HEAD origin/main --; then
    echo "==> No changes between HEAD and origin/main. Nothing to deploy."
    exit 0
fi

mapfile -t CHANGED_FILES < <(git diff --name-only HEAD origin/main)
echo "==> Changed files since HEAD:"
printf '    - %s\n' "${CHANGED_FILES[@]}"

# Full-rebuild patterns: infrastructure, schema, or anything the
# web/worker/beat services must pick up on restart.
FULL_PATTERNS=(
    '^docker-compose\.yml$'
    '^docker-compose\.prod\.yml$'
    '^Dockerfile$'
    'Dockerfile\.[[:alnum:]]+$'
    '^requirements.*\.txt$'
    '^requirements/'
    '^data/migrations/'
    '^brazos_cad/migrations/'
    '^data/models\.py$'
    '^brazos_cad/models\.py$'
    '^taxprotest/settings\.py$'
    '^taxprotest/runtime_paths\.py$'
    '^taxprotest/celery\.py$'
    '^taxprotest/urls\.py$'
    '^templates/'
    '^static/'
)

# Partial-rebuild patterns: code that is only run by the `etl` service.
# If web/worker/beat ever import any of these paths, classify as FULL.
PARTIAL_PATTERNS=(
    '^brazos_cad/management/'
    '^brazos_cad/parsers/'
    '^brazos_cad/etl\.py$'
    '^data/etl_pipeline/'
    '^data/etl\.py$'
)

# Skip patterns: docs, CI config, tests, repo housekeeping only.
SKIP_PATTERNS=(
    '\.md$'
    '^docs/'
    '^LICENSE'
    '^\.github/'
    '^tests/'
    '\.test\.py$'
    '^scripts/monitor_'
    '^scripts/benchmark_'
    '^scripts/check_'
    '^scripts/run_gis_'
)

matches_any() {
    local file="$1"; shift
    local pattern
    for pattern in "$@"; do
        if [[ "$file" =~ $pattern ]]; then
            return 0
        fi
    done
    return 1
}

classification="FULL"
for f in "${CHANGED_FILES[@]}"; do
    if matches_any "$f" "${FULL_PATTERNS[@]}"; then
        classification="FULL"
        break
    fi
done

if [[ "$classification" == "FULL" ]]; then
    :
else
    # PARTIAL only if every change is in PARTIAL_PATTERNS (no mixed changes).
    all_partial=true
    for f in "${CHANGED_FILES[@]}"; do
        if ! matches_any "$f" "${PARTIAL_PATTERNS[@]}"; then
            all_partial=false
            break
        fi
    done
    if $all_partial; then
        classification="PARTIAL"
    else
        # Everything else: SKIP only if every change is in SKIP_PATTERNS.
        all_skip=true
        for f in "${CHANGED_FILES[@]}"; do
            if ! matches_any "$f" "${SKIP_PATTERNS[@]}"; then
                all_skip=false
                break
            fi
        done
        if $all_skip; then
            classification="SKIP"
        else
            classification="FULL"
        fi
    fi
fi

echo "==> Classification: $classification"

# --- pull + rebuild ----------------------------------------------------------

git pull --ff-only origin main

case "$classification" in
    FULL)
        echo "==> Full rebuild: docker compose up -d --build"
        docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
        ;;
    PARTIAL)
        echo "==> Partial rebuild: docker compose up -d --build etl"
        if docker compose -f docker-compose.yml config --services 2>/dev/null | grep -qx 'etl'; then
            docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build etl
            echo "    NOTE: only the 'etl' service image was rebuilt."
            echo "          web/worker/beat are running the previous image until their next restart."
        else
            echo "    WARN: 'etl' service is not defined in compose; falling back to full rebuild."
            docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
        fi
        ;;
    SKIP)
        echo "==> Skipping rebuild (docs/comments/CI only)."
        ;;
esac

# --- image prune -------------------------------------------------------------

echo "==> Pruning dangling build layers"
docker image prune -f

echo "==> Deploy complete"
