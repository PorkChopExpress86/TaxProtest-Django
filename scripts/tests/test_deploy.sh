#!/usr/bin/env bash
# Focused behavior checks for scripts/deploy.sh. Runs with only Bash and fake
# git/docker executables; it never contacts a remote or Docker daemon.

set -euo pipefail

SCRIPT_DIRECTORY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_SCRIPT="$(cd "$SCRIPT_DIRECTORY/.." && pwd)/deploy.sh"
DEPLOY_WORKFLOW="$(cd "$SCRIPT_DIRECTORY/../.." && pwd)/.github/workflows/deploy.yml"
TEST_ROOT="$(mktemp -d)"
readonly SOURCE_REVISION="1111111111111111111111111111111111111111"
readonly TARGET_REVISION="2222222222222222222222222222222222222222"

cleanup() {
    rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

assert_file_contains() {
    local file="$1"
    local expected="$2"
    grep -Fqx "$expected" "$file" || fail "expected '$expected' in $file"
}

assert_file_does_not_contain() {
    local file="$1"
    local unexpected="$2"
    if grep -Fq "$unexpected" "$file"; then
        fail "did not expect '$unexpected' in $file"
    fi
}

assert_file_contains_text() {
    local file="$1"
    local expected="$2"
    grep -Fq "$expected" "$file" || fail "expected text '$expected' in $file"
}

prepare_case() {
    local case_name="$1"
    local changed_path="$2"
    local case_root="$TEST_ROOT/$case_name"

    CASE_PROJECT="$case_root/project"
    CASE_BIN="$case_root/bin"
    CASE_STATE="$case_root/state"
    CASE_LOG="$case_root/docker.log"
    CASE_READINESS_STATUS=0
    mkdir -p "$CASE_PROJECT/.git" "$CASE_PROJECT/scripts" "$CASE_BIN" "$CASE_STATE"
    cp "$SOURCE_SCRIPT" "$CASE_PROJECT/scripts/deploy.sh"
    chmod +x "$CASE_PROJECT/scripts/deploy.sh"
    : > "$CASE_PROJECT/docker-compose.yml"
    : > "$CASE_PROJECT/docker-compose.prod.yml"
    printf '%s\n' "$SOURCE_REVISION" > "$CASE_STATE/head"
    printf '%s\n' "$TARGET_REVISION" > "$CASE_STATE/target"
    printf '%s\n' "$changed_path" > "$CASE_STATE/changed-files"
    : > "$CASE_LOG"

    cat > "$CASE_BIN/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

state="${FAKE_GIT_STATE:?}"
case "$1" in
    fetch)
        exit 0
        ;;
    rev-parse)
        case "$2" in
            --is-inside-work-tree)
                echo true
                ;;
            --show-toplevel)
                printf '%s\n' "${PROJECT_PATH:?}"
                ;;
            HEAD)
                cat "$state/head"
                ;;
            origin/main)
                cat "$state/target"
                ;;
            *)
                exit 1
                ;;
        esac
        ;;
    cat-file)
        exit 0
        ;;
    merge-base)
        exit 0
        ;;
    diff)
        if [[ "$2" == "--quiet" || ( "$2" == "--cached" && "$3" == "--quiet" ) ]]; then
            exit 0
        fi
        if [[ "$2" == "--name-only" ]]; then
            cat "$state/changed-files"
            exit 0
        fi
        exit 1
        ;;
    merge)
        cp "$state/target" "$state/head"
        ;;
    reset)
        cp "$state/target" "$state/head"
        ;;
    *)
        echo "unexpected fake git invocation: $*" >&2
        exit 1
        ;;
esac
EOF
    chmod +x "$CASE_BIN/git"

    cat > "$CASE_BIN/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >> "${FAKE_DOCKER_LOG:?}"
if [[ "$1" == "compose" && "$2" == "version" ]]; then
    exit 0
fi
if [[ " $* " == *" exec "* ]]; then
    exit "${FAKE_READINESS_STATUS:-0}"
fi
exit 0
EOF
    chmod +x "$CASE_BIN/docker"

    cat > "$CASE_BIN/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "$CASE_BIN/sleep"
}

run_deploy() {
    PATH="$CASE_BIN:$PATH" \
        PROJECT_PATH="$CASE_PROJECT" \
        FAKE_GIT_STATE="$CASE_STATE" \
        FAKE_DOCKER_LOG="$CASE_LOG" \
        FAKE_READINESS_STATUS="$CASE_READINESS_STATUS" \
        bash < "$CASE_PROJECT/scripts/deploy.sh"
}

test_missing_state_forces_full_rebuild() {
    prepare_case "missing-state" "docs/release-notes.md"
    run_deploy

    assert_file_contains "$CASE_LOG" "compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"
    assert_file_contains "$CASE_LOG" "compose -f docker-compose.yml -f docker-compose.prod.yml exec -T web python -c from urllib.request import urlopen; response = urlopen(\"http://127.0.0.1:8000/readiness/\", timeout=5); raise SystemExit(0 if response.status == 200 else 1)"
    assert_file_contains "$CASE_LOG" "compose -f docker-compose.yml -f docker-compose.prod.yml exec -T web python manage.py check_deployment_readiness"
    assert_file_contains "$CASE_LOG" "image prune -f"
    assert_file_contains "$CASE_PROJECT/.deploy-state/last-complete-revision" "$TARGET_REVISION"
}

test_docs_only_change_skips_rebuild_and_advances_state() {
    prepare_case "skip" "docs/release-notes.md"
    mkdir -p "$CASE_PROJECT/.deploy-state"
    printf '%s\n' "$SOURCE_REVISION" > "$CASE_PROJECT/.deploy-state/last-complete-revision"

    run_deploy

    assert_file_does_not_contain "$CASE_LOG" " up -d --build"
    assert_file_does_not_contain "$CASE_LOG" " exec -T web "
    assert_file_contains "$CASE_PROJECT/.deploy-state/last-complete-revision" "$TARGET_REVISION"
}

test_former_partial_path_now_forces_full_rebuild() {
    prepare_case "former-partial" "counties/harris/etl_pipeline/orchestrator.py"
    mkdir -p "$CASE_PROJECT/.deploy-state"
    printf '%s\n' "$SOURCE_REVISION" > "$CASE_PROJECT/.deploy-state/last-complete-revision"

    run_deploy

    assert_file_contains "$CASE_LOG" "compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"
    assert_file_does_not_contain "$CASE_LOG" " up -d --build etl"
}

test_no_change_is_a_no_op() {
    prepare_case "no-change" "docs/ignored.md"
    printf '%s\n' "$TARGET_REVISION" > "$CASE_STATE/head"
    mkdir -p "$CASE_PROJECT/.deploy-state"
    printf '%s\n' "$TARGET_REVISION" > "$CASE_PROJECT/.deploy-state/last-complete-revision"

    run_deploy

    assert_file_contains "$CASE_LOG" "compose version"
    assert_file_does_not_contain "$CASE_LOG" " up -d --build"
    assert_file_does_not_contain "$CASE_LOG" " exec -T web "
}

test_readiness_failure_keeps_prior_state() {
    prepare_case "readiness-failure" "taxprotest/settings.py"
    mkdir -p "$CASE_PROJECT/.deploy-state"
    printf '%s\n' "$SOURCE_REVISION" > "$CASE_PROJECT/.deploy-state/last-complete-revision"
    sed -i 's/readonly READINESS_ATTEMPTS=180/readonly READINESS_ATTEMPTS=1/' "$CASE_PROJECT/scripts/deploy.sh"
    CASE_READINESS_STATUS=1

    if run_deploy; then
        fail "expected readiness failure"
    fi

    assert_file_contains "$CASE_PROJECT/.deploy-state/last-complete-revision" "$SOURCE_REVISION"
    assert_file_contains "$CASE_LOG" "compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build"
}

test_workflow_runs_the_target_revision_deployment_module() {
    [[ -f "$DEPLOY_WORKFLOW" ]] || fail "deployment workflow not found"

    assert_file_contains_text "$DEPLOY_WORKFLOW" \
        'git fetch --quiet origin main && mkdir -p .deploy-state && git show origin/main:scripts/deploy.sh > .deploy-state/target-deploy.sh && bash .deploy-state/target-deploy.sh && rm -f .deploy-state/target-deploy.sh'
}

test_missing_state_forces_full_rebuild
test_docs_only_change_skips_rebuild_and_advances_state
test_former_partial_path_now_forces_full_rebuild
test_no_change_is_a_no_op
test_readiness_failure_keeps_prior_state
test_workflow_runs_the_target_revision_deployment_module
echo "PASS: deployment module behavior"
