#!/usr/bin/env bash
# Throwaway verification instance of this checkout: own Compose project, own
# postgres volume, seeded data, Django runserver on 127.0.0.1:$VERIFY_PORT.
#
#   .claude/skills/verify/scripts/verify.sh up         # migrate, seed, serve; waits until usable
#   .claude/skills/verify/scripts/verify.sh up --build # rebuild the image first (dependency/Dockerfile changes)
#   .claude/skills/verify/scripts/verify.sh doctor     # read-only: is this instance worth driving?
#   .claude/skills/verify/scripts/verify.sh evidence <name>   # make and print an evidence directory
#   .claude/skills/verify/scripts/verify.sh logs       # runserver + seed output
#   .claude/skills/verify/scripts/verify.sh down       # remove this instance and its database; keeps evidence
set -euo pipefail

skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
root="$(cd "$skill_dir/../../.." && pwd)"
slug="$(basename "$root" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9\n' '-')"
project="tpverify-${slug}"
port="${VERIFY_PORT:-18020}"
base_url="http://127.0.0.1:${port}"

# Fixed throwaway credentials: shell values win over any .env, so the main
# checkout's real settings never reach this instance.
export POSTGRES_USER=taxprotest POSTGRES_PASSWORD=verify POSTGRES_DB=taxprotest
export DJANGO_SECRET_KEY=verify-only-not-a-secret
export VERIFY_PORT="$port" SKIP_DATA_DOWNLOAD=1

compose() {
  docker compose -p "$project" --project-directory "$root" \
    -f "$root/docker-compose.yml" -f "$skill_dir/compose.verify.yml" \
    --profile devtools "$@"
}

http_status() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" || true
}

expect_200() {
  if [ "$1" = 200 ]; then
    echo "ok    $2 200"
  else
    echo "FAIL  $2 $1"
    return 1
  fi
}

doctor() {
  local ok=0 container mount
  container="$(compose ps -q taxprotest-dev 2>/dev/null | head -1)"
  if [ -z "$container" ]; then
    echo "FAIL  no running taxprotest-dev container in project $project (run: verify.sh up)"
    return 1
  fi
  echo "ok    project $project, container ${container:0:12}"

  mount="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/app"}}{{.Source}}{{end}}{{end}}' "$container")"
  echo "info  /app is mounted from $mount (expected this checkout: $root)"

  if [ "$(compose port taxprotest-dev 8000 2>/dev/null)" = "127.0.0.1:${port}" ]; then
    echo "ok    port ${port} belongs to this instance"
  else
    echo "FAIL  port ${port} is not published by this instance"; ok=1
  fi

  local health index similar
  health="$(http_status "$base_url/healthz/")"
  index="$(http_status "$base_url/")"
  similar="$(http_status "$base_url/similar/HT0000000001/")"
  expect_200 "$health" "/healthz/ (database reachable)" || ok=1
  expect_200 "$index" "/" || ok=1
  expect_200 "$similar" "/similar/HT0000000001/ (seeded subject)" || ok=1
  git -C "$root" log -1 --format='info  checkout HEAD %h %s' || true
  return "$ok"
}

case "${1:-}" in
  up)
    if [ "$(http_status "$base_url/healthz/")" != 000 ] && [ -z "$(compose ps -q taxprotest-dev 2>/dev/null)" ]; then
      echo "Port ${port} already answers and is not this instance. Set VERIFY_PORT to a free port." >&2
      exit 1
    fi
    build=()
    if [ "${2:-}" = "--build" ] || ! docker image inspect "${project}-taxprotest-dev" >/dev/null 2>&1; then
      build=(--build)
    fi
    compose up -d ${build[@]+"${build[@]}"} postgres taxprotest-dev
    echo "Waiting for migrate + seed + runserver on $base_url (minutes on a Windows bind mount) ..."
    deadline=$((SECONDS + 900))
    while [ "$SECONDS" -lt "$deadline" ]; do
      if [ "$(http_status "$base_url/similar/HT0000000001/")" = 200 ]; then
        echo "READY $base_url"
        exit 0
      fi
      if [ -z "$(compose ps -q --status running taxprotest-dev 2>/dev/null)" ]; then
        echo "taxprotest-dev exited before becoming ready:" >&2
        compose logs --tail 40 taxprotest-dev >&2
        exit 1
      fi
      sleep 2
    done
    echo "Timed out waiting for $base_url" >&2
    compose logs --tail 40 taxprotest-dev >&2
    exit 1
    ;;
  doctor)
    doctor
    ;;
  evidence)
    name="${2:?usage: verify.sh evidence <name>}"
    dir="$root/.scratch/verify/$(date -u +%Y%m%dT%H%M%SZ)-${name}"
    mkdir -p "$dir"
    echo "$dir"
    ;;
  logs)
    compose logs --tail "${2:-80}" taxprotest-dev
    ;;
  down)
    compose down -v --remove-orphans
    ;;
  url)
    echo "$base_url"
    ;;
  *)
    sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
