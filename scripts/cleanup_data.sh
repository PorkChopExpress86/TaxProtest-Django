#!/usr/bin/env bash
#
# Reclaim disk used by ETL staging artifacts.
#
# `load_brazos_cad` already cleans up after itself on success. This script is for
# the leftovers: a failed or interrupted run, files staged before automatic
# cleanup existed, and the HCAD pipeline's caches.
#
# Tiers, in increasing order of what they cost you to rebuild:
#
#   (default)    Extracted/derived files only. Regenerated from the local
#                archives — no network needed.
#   --archives   Also the downloaded .zip archives. Rebuilding these re-downloads
#                ~800 MB from HCAD and Brazos CAD.
#   --all        Both of the above.
#
# Scope, to clean one pipeline without touching the other:
#
#   --brazos     Brazos CAD staging only (data/cad_downloads/)
#   --hcad       HCAD staging only (var/)
#
# Nothing here touches the database: every target is a cache, and the imported
# rows in PostgreSQL are the durable copy. Tracked .gitkeep files are preserved.
#
# Usage:
#   ./scripts/cleanup_data.sh --dry-run     # report what would go (do this first)
#   ./scripts/cleanup_data.sh               # extracted data only
#   ./scripts/cleanup_data.sh --brazos --all # everything Brazos CAD staged
#   ./scripts/cleanup_data.sh --all --yes   # everything, no prompt

set -euo pipefail

DRY_RUN=0
INCLUDE_ARCHIVES=0
INCLUDE_EXTRACTED=1
ASSUME_YES=0
SCOPE=both

usage() {
    sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^#\s\?//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--dry-run)  DRY_RUN=1 ;;
        --archives)    INCLUDE_ARCHIVES=1; INCLUDE_EXTRACTED=0 ;;
        --all)         INCLUDE_ARCHIVES=1; INCLUDE_EXTRACTED=1 ;;
        --brazos)      SCOPE=brazos ;;
        --hcad)        SCOPE=hcad ;;
        -y|--yes)      ASSUME_YES=1 ;;
        -h|--help)     usage 0 ;;
        *)             printf 'Unknown option: %s\n\n' "$1" >&2; usage 1 ;;
    esac
    shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

log() { printf '[cleanup] %s\n' "$*"; }

# Directories whose *contents* are removed. .gitkeep is tracked in git and stays.
EXTRACTED_DIRS=()
ARCHIVE_DIRS=()
if [[ "$SCOPE" == "hcad" || "$SCOPE" == "both" ]]; then
    EXTRACTED_DIRS+=("var/extracted")   # unpacked Real_acct_owner / Parcels / building_land
    ARCHIVE_DIRS+=("var/downloads")     # HCAD source .zip archives
fi
if [[ "$SCOPE" == "brazos" || "$SCOPE" == "both" ]]; then
    EXTRACTED_DIRS+=("data/cad_downloads/extracted")  # unpacked PACS .TXT files
    EXTRACTED_DIRS+=("data/cad_downloads/gis")        # unpacked parcel shapefiles
    ARCHIVE_DIRS+=("data/cad_downloads")              # Brazos CAD source .zip archives
fi

targets=()
[[ $INCLUDE_EXTRACTED -eq 1 ]] && targets+=("${EXTRACTED_DIRS[@]}")
[[ $INCLUDE_ARCHIVES  -eq 1 ]] && targets+=("${ARCHIVE_DIRS[@]}")

# `du` on a missing path is an error, so filter to what exists first.
present=()
for dir in "${targets[@]}"; do
    [[ -d "$dir" ]] && present+=("$dir")
done

if [[ ${#present[@]} -eq 0 ]]; then
    log "nothing to clean — no staging directories present."
    exit 0
fi

log "project root: $PROJECT_ROOT"
log "targets:"
total_kb=0
for dir in "${present[@]}"; do
    # For archive dirs, only the top-level files count; the nested extracted/
    # subdirectory is reported under its own entry.
    if [[ " ${ARCHIVE_DIRS[*]} " == *" $dir "* && $INCLUDE_ARCHIVES -eq 1 ]]; then
        kb=$(find "$dir" -maxdepth 1 -type f ! -name '.gitkeep' -print0 2>/dev/null \
             | du -sc --files0-from=- 2>/dev/null | tail -1 | cut -f1 || echo 0)
    else
        kb=$(du -sk "$dir" 2>/dev/null | cut -f1 || echo 0)
    fi
    kb=${kb:-0}
    total_kb=$((total_kb + kb))
    printf '  %8s  %s\n' "$(numfmt --to=iec --from-unit=1024 "$kb" 2>/dev/null || echo "${kb}K")" "$dir"
done

log "reclaimable: $(numfmt --to=iec --from-unit=1024 "$total_kb" 2>/dev/null || echo "${total_kb}K")"

if [[ $DRY_RUN -eq 1 ]]; then
    log "dry run — nothing removed."
    exit 0
fi

if [[ $ASSUME_YES -ne 1 ]]; then
    read -r -p "[cleanup] Remove these? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || { log "aborted."; exit 0; }
fi

for dir in "${present[@]}"; do
    if [[ " ${ARCHIVE_DIRS[*]} " == *" $dir "* && $INCLUDE_ARCHIVES -eq 1 ]]; then
        # Top-level files only: leave .gitkeep and any extracted/ subdirectory,
        # which is handled by its own entry when --all is used.
        find "$dir" -maxdepth 1 -type f ! -name '.gitkeep' -delete
        log "removed archives in $dir"
    fi
    if [[ " ${EXTRACTED_DIRS[*]} " == *" $dir "* && $INCLUDE_EXTRACTED -eq 1 ]]; then
        # -depth so directories are removed after their contents.
        find "$dir" -depth -mindepth 1 ! -name '.gitkeep' -delete 2>/dev/null || true
        log "emptied $dir"
    fi
done

log "done. Disk now:"
df -h "$PROJECT_ROOT" | tail -1 | sed 's/^/  /'
