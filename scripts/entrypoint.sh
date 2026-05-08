#!/bin/bash
set -e

echo "Starting TaxProtest-Django..."

# Run migrations (idempotent — safe for all services)
echo "Running migrations..."
python manage.py migrate --noinput

# Worker, beat, or any other custom command — skip data loading and run it directly.
if [ $# -gt 0 ]; then
    exec "$@"
fi

# Web server startup path below.
#
# If fresh build-baked downloads exist at /hcad_downloads_baked (placed there
# by `docker build`), compare the build stamp against what was last synced.
# Only copy and re-import when the build stamp has changed — avoids copying
# several GB of HCAD data on every container restart.

BAKED_DIR=/hcad_downloads_baked
BAKED_STAMP=$(cat "$BAKED_DIR/.build_stamp" 2>/dev/null || echo "")
RUNTIME_ROOT="${RUNTIME_ROOT:-/app/var}"
HCAD_DOWNLOAD_DIR="${HCAD_DOWNLOAD_DIR:-$RUNTIME_ROOT/downloads}"
SYNCED_STAMP=$(cat "$HCAD_DOWNLOAD_DIR/.synced_stamp" 2>/dev/null || echo "")

if [ -n "$BAKED_STAMP" ] && [ "$BAKED_STAMP" != "$SYNCED_STAMP" ]; then
    echo "Fresh build detected (stamp: $BAKED_STAMP) — syncing to $HCAD_DOWNLOAD_DIR..."
    mkdir -p "$HCAD_DOWNLOAD_DIR"
    if cp -a "$BAKED_DIR/." "$HCAD_DOWNLOAD_DIR/"; then
        echo "Importing data (full overwrite of existing database records)..."
        python manage.py import_all_data --skip-download --skip-contract-validation
        echo "$BAKED_STAMP" > "$HCAD_DOWNLOAD_DIR/.synced_stamp"
    else
        echo "WARNING: Could not sync baked downloads to $HCAD_DOWNLOAD_DIR (likely permission issue)."
        echo "WARNING: Continuing startup and falling back to runtime data check/import path."
        python manage.py check_and_import_data
    fi
else
    # Same build as last start, or SKIP_DATA_DOWNLOAD=1 — only import if DB is empty.
    echo "Checking data..."
    python manage.py check_and_import_data
fi

echo "Starting web server..."
exec gunicorn taxprotest.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers ${WEB_CONCURRENCY:-3} \
    --worker-tmp-dir /dev/shm \
    --no-control-socket \
    --timeout 60 \
    --max-requests 1000 \
    --max-requests-jitter 100
