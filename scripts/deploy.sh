#!/usr/bin/env bash
# Deployment module for TaxProtest-Django.
#
# The external entry point snapshots the deployment-complete and target
# revisions, chooses a plan, synchronizes the checkout, then re-executes the
# target revision of this module to apply that plan. This keeps deployment
# policy local and makes policy changes effective in the delivery run.

set -euo pipefail

readonly STATE_DIRECTORY_NAME=".deploy-state"
readonly STATE_FILE_NAME="last-complete-revision"
# A full image with baked HCAD data can take roughly eleven minutes to start.
readonly READINESS_ATTEMPTS=180
readonly READINESS_DELAY_SECONDS=5

FULL_PATTERNS=(
    '^docker-compose\.yml$'
    '^docker-compose\.prod\.yml$'
    '^Dockerfile$'
    'Dockerfile\.[[:alnum:]]+$'
    '^requirements.*\.txt$'
    '^requirements/'
    '^counties/[^/]+/migrations/'
    '^counties/[^/]+/models\.py$'
    # The shared web module and every county's adapter/urls are imported by
    # web/worker/beat, so any change needs a full rebuild. Matched
    # module-by-module so counties/common/tests/ stays skippable.
    '^counties/common/[^/]+\.py$'
    '^counties/common/templatetags/'
    '^counties/[^/]+/adapter\.py$'
    '^counties/[^/]+/urls\.py$'
    '^counties/[^/]+/apps\.py$'
    '^counties/[^/]+/similarity\.py$'
    '^counties/[^/]+/query\.py$'
    '^counties/[^/]+/tax_impact\.py$'
    '^counties/[^/]+/assessment_history\.py$'
    '^counties/[^/]+/tasks_new\.py$'
    '^counties/[^/]+/admin\.py$'
    '^taxprotest/settings\.py$'
    '^taxprotest/runtime_paths\.py$'
    '^taxprotest/celery\.py$'
    '^taxprotest/urls\.py$'
    '^taxprotest/views\.py$'
    '^templates/'
    '^static/'
)

# Only paths with no runtime effect can skip a rebuild. All former PARTIAL
# paths are deliberately FULL until process ownership can be proven exactly.
SKIP_PATTERNS=(
    '\.md$'
    '^docs/'
    '^LICENSE'
    '^\.github/'
    '^counties/[^/]+/tests/'
    '\.test\.py$'
    '^scripts/manual/'
    '^scripts/monitor_'
    '^scripts/benchmark_'
    '^scripts/check_'
    '^scripts/run_gis_'
    # Runtime data directories carry only a .gitignore in version control.
    '^counties/[^/]+/var/'
    '^taxprotest/var/'
)

die() {
    echo "ERROR: $*" >&2
    exit 1
}

usage() {
    cat >&2 <<'EOF'
Usage: scripts/deploy.sh
       scripts/deploy.sh --apply --from <revision> --to <revision> [--force-full <reason>]
EOF
}

matches_any() {
    local file="$1"
    shift
    local pattern
    for pattern in "$@"; do
        if [[ "$file" =~ $pattern ]]; then
            return 0
        fi
    done
    return 1
}

require_dependencies() {
    local command_name
    for command_name in git docker; do
        command -v "$command_name" >/dev/null 2>&1 || die "required command not found in PATH: $command_name"
    done

    docker compose version >/dev/null 2>&1 || die "'docker compose' plugin not available; install Docker Compose v2+"
}

initialize_project() {
    PROJECT_PATH="${PROJECT_PATH:-$(pwd)}"
    cd "$PROJECT_PATH"
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$PROJECT_PATH is not a git working tree"
    PROJECT_PATH="$(git rev-parse --show-toplevel)"
    export PROJECT_PATH

    STATE_DIRECTORY="$PROJECT_PATH/$STATE_DIRECTORY_NAME"
    STATE_FILE="$STATE_DIRECTORY/$STATE_FILE_NAME"
}

read_deployment_complete_revision() {
    [[ -f "$STATE_FILE" ]] || return 1

    local revision
    IFS= read -r revision < "$STATE_FILE" || true
    if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]] || ! git cat-file -e "${revision}^{commit}" 2>/dev/null; then
        echo "==> Warning: deployment state is invalid; selecting a full rebuild." >&2
        return 1
    fi

    printf '%s\n' "$revision"
}

write_deployment_complete_revision() {
    local target_revision="$1"
    local temporary_file

    umask 077
    mkdir -p "$STATE_DIRECTORY"
    temporary_file="$STATE_FILE.tmp.$$"
    printf '%s\n' "$target_revision" > "$temporary_file"
    mv -f "$temporary_file" "$STATE_FILE"
}

worktree_is_clean() {
    git diff --quiet -- && git diff --cached --quiet --
}

synchronize_checkout() {
    local target_revision="$1"
    local current_revision

    current_revision="$(git rev-parse HEAD)"
    if [[ "$current_revision" == "$target_revision" ]] && worktree_is_clean; then
        return
    fi

    if worktree_is_clean && git merge-base --is-ancestor "$current_revision" "$target_revision"; then
        echo "==> Fast-forwarding checkout to ${target_revision:0:12}"
        git merge --ff-only "$target_revision"
    else
        echo "==> Warning: checkout cannot fast-forward cleanly. Resetting to ${target_revision:0:12}..."
        git reset --hard "$target_revision"
    fi

    [[ "$(git rev-parse HEAD)" == "$target_revision" ]] || die "checkout did not reach the target revision"
    worktree_is_clean || die "checkout remains modified after synchronization"
}

classify_revision_range() {
    local from_revision="$1"
    local target_revision="$2"
    local -a changed_files=()
    local file
    local all_skip=true

    mapfile -t changed_files < <(git diff --name-only "$from_revision" "$target_revision")
    if ((${#changed_files[@]} == 0)); then
        printf '%s\n' "SKIP"
        return
    fi

    {
        echo "==> Changed files since deployment-complete revision:"
        printf '    - %s\n' "${changed_files[@]}"
    } >&2

    for file in "${changed_files[@]}"; do
        if matches_any "$file" "${FULL_PATTERNS[@]}"; then
            printf '%s\n' "FULL"
            return
        fi
        if ! matches_any "$file" "${SKIP_PATTERNS[@]}"; then
            all_skip=false
        fi
    done

    if [[ "$all_skip" == true ]]; then
        printf '%s\n' "SKIP"
    else
        printf '%s\n' "FULL"
    fi
}

wait_for_readiness() {
    local attempt

    echo "==> Waiting for web readiness"
    for ((attempt = 1; attempt <= READINESS_ATTEMPTS; attempt++)); do
        if docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T web \
            python -c 'from urllib.request import urlopen; response = urlopen("http://127.0.0.1:8000/readiness/", timeout=5); raise SystemExit(0 if response.status == 200 else 1)' \
            >/dev/null 2>&1; then
            echo "==> Web readiness confirmed"
            return
        fi
        if ((attempt % 12 == 0)); then
            echo "    readiness is not available yet (attempt $attempt/$READINESS_ATTEMPTS)"
        fi
        sleep "$READINESS_DELAY_SECONDS"
    done

    die "web did not become ready before the deployment timeout"
}

apply_plan() {
    local from_revision="$1"
    local target_revision="$2"
    local force_full_reason="$3"
    local classification

    [[ "$(git rev-parse HEAD)" == "$target_revision" ]] || die "target checkout changed before applying the deployment plan"

    if [[ -n "$force_full_reason" ]]; then
        classification="FULL"
        echo "==> Classification: FULL ($force_full_reason)"
    else
        classification="$(classify_revision_range "$from_revision" "$target_revision")"
        echo "==> Classification: $classification"
    fi

    case "$classification" in
        FULL)
            echo "==> Full rebuild: docker compose up -d --build"
            docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
            wait_for_readiness
            echo "==> Pruning dangling build layers"
            docker image prune -f
            ;;
        SKIP)
            echo "==> Skipping rebuild (runtime-irrelevant changes only)."
            ;;
        *)
            die "unsupported deployment classification: $classification"
            ;;
    esac

    write_deployment_complete_revision "$target_revision"
    echo "==> Deployment complete: ${target_revision:0:12}"
}

plan_deployment() {
    local target_revision
    local deployment_complete_revision
    local from_revision
    local force_full_reason=""
    local current_revision
    local -a apply_arguments

    git fetch --quiet origin main
    target_revision="$(git rev-parse origin/main)"
    current_revision="$(git rev-parse HEAD)"

    echo "==> Deploying in $PROJECT_PATH"
    echo "    HEAD:   ${current_revision:0:12}"
    echo "    target: ${target_revision:0:12}"

    if deployment_complete_revision="$(read_deployment_complete_revision)"; then
        from_revision="$deployment_complete_revision"
        if ! git merge-base --is-ancestor "$from_revision" "$target_revision"; then
            force_full_reason="nonlinear-history"
        fi
    else
        from_revision="$target_revision"
        force_full_reason="missing-deployment-state"
    fi

    if [[ -z "$force_full_reason" && "$from_revision" == "$target_revision" && "$current_revision" == "$target_revision" ]] && worktree_is_clean; then
        echo "==> Target is already deployment-complete. Nothing to deploy."
        return
    fi

    if [[ -z "$force_full_reason" && "$from_revision" == "$target_revision" ]]; then
        force_full_reason="checkout-drift"
    fi
    if ! worktree_is_clean && [[ -z "$force_full_reason" ]]; then
        force_full_reason="modified-checkout"
    fi

    synchronize_checkout "$target_revision"

    apply_arguments=(--apply --from "$from_revision" --to "$target_revision")
    if [[ -n "$force_full_reason" ]]; then
        apply_arguments+=(--force-full "$force_full_reason")
    fi

    exec "$PROJECT_PATH/scripts/deploy.sh" "${apply_arguments[@]}"
}

MODE="plan"
FROM_REVISION=""
TARGET_REVISION=""
FORCE_FULL_REASON=""

while (($# > 0)); do
    case "$1" in
        --apply)
            [[ "$MODE" == "plan" ]] || die "--apply may be provided only once"
            MODE="apply"
            shift
            ;;
        --from)
            (($# >= 2)) || die "--from requires a revision"
            FROM_REVISION="$2"
            shift 2
            ;;
        --to)
            (($# >= 2)) || die "--to requires a revision"
            TARGET_REVISION="$2"
            shift 2
            ;;
        --force-full)
            (($# >= 2)) || die "--force-full requires a reason"
            FORCE_FULL_REASON="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            usage
            die "unknown argument: $1"
            ;;
    esac
done

require_dependencies
initialize_project

if [[ "$MODE" == "apply" ]]; then
    [[ -n "$FROM_REVISION" && -n "$TARGET_REVISION" ]] || die "--apply requires --from and --to"
    git cat-file -e "${FROM_REVISION}^{commit}" 2>/dev/null || die "unknown source revision: $FROM_REVISION"
    git cat-file -e "${TARGET_REVISION}^{commit}" 2>/dev/null || die "unknown target revision: $TARGET_REVISION"
    apply_plan "$FROM_REVISION" "$TARGET_REVISION" "$FORCE_FULL_REASON"
else
    [[ -z "$FROM_REVISION$TARGET_REVISION$FORCE_FULL_REASON" ]] || die "internal deployment arguments require --apply"
    plan_deployment
fi
