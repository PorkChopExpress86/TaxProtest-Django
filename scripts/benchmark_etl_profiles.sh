#!/usr/bin/env bash
set -euo pipefail

OUTDIR="${1:-reports/etl-benchmarks/2026-05-02}"
mkdir -p "$OUTDIR"

run_stage() {
  local label="$1"
  local container_name="$2"
  shift 2

  local log_file="$OUTDIR/${label}.log"
  local stats_file="$OUTDIR/${label}.stats"
  local meta_file="$OUTDIR/${label}.meta"

  : > "$stats_file"
  : > "$log_file"

  (
    local started=0
    while true; do
      if docker container inspect "$container_name" >/dev/null 2>&1; then
        started=1
        local ts
        ts="$(date +%s)"
        local line
        line="$(docker stats "$container_name" --no-stream --format '{{.CPUPerc}}|{{.MemUsage}}' 2>/dev/null || true)"
        if [[ -n "$line" ]]; then
          printf '%s|%s\n' "$ts" "$line" >> "$stats_file"
        fi
        sleep 5
      else
        if [[ "$started" -eq 1 ]]; then
          break
        fi
        sleep 1
      fi
    done
  ) &
  local sampler_pid=$!

  local start_epoch
  local end_epoch
  local start_iso
  local end_iso
  local exit_code

  start_epoch="$(date +%s)"
  start_iso="$(date -Iseconds)"

  set +e
  /usr/bin/time -p docker compose run --name "$container_name" --rm --user root web "$@" >"$log_file" 2>&1
  exit_code=$?
  set -e

  end_epoch="$(date +%s)"
  end_iso="$(date -Iseconds)"

  wait "$sampler_pid" || true

  {
    printf 'label=%s\n' "$label"
    printf 'container=%s\n' "$container_name"
    printf 'start_iso=%s\n' "$start_iso"
    printf 'end_iso=%s\n' "$end_iso"
    printf 'start_epoch=%s\n' "$start_epoch"
    printf 'end_epoch=%s\n' "$end_epoch"
    printf 'elapsed_seconds=%s\n' "$((end_epoch - start_epoch))"
    printf 'exit_code=%s\n' "$exit_code"
  } > "$meta_file"

  if [[ "$exit_code" -ne 0 ]]; then
    echo "Stage ${label} failed; see ${log_file}" >&2
    return "$exit_code"
  fi
}

run_stage "legacy_property" "bench_legacy_property" \
  python manage.py load_hcad_real_acct --no-refresh-readiness

run_stage "legacy_building" "bench_legacy_building" \
  python manage.py import_building_data --skip-download --no-refresh-readiness

run_stage "legacy_gis" "bench_legacy_gis" \
  python manage.py load_gis_data --skip-download --no-refresh-readiness

run_stage "modern_property_stage" "bench_modern_property" \
  python manage.py etl_pipeline run --skip-download --skip-extract --property-only

run_stage "modern_gis_stage" "bench_modern_gis" \
  python manage.py etl_pipeline run --skip-download --skip-extract --gis-only

python3 - "$OUTDIR" <<'PY'
import json
import math
import re
import sys
from pathlib import Path

outdir = Path(sys.argv[1])
labels = [
    "legacy_property",
    "legacy_building",
    "legacy_gis",
    "modern_property_stage",
    "modern_gis_stage",
]

def parse_meta(path: Path):
    data = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v
    data["elapsed_seconds"] = int(data.get("elapsed_seconds", "0"))
    data["exit_code"] = int(data.get("exit_code", "1"))
    return data

def parse_mem_to_mib(mem_token: str) -> float:
    m = re.match(r"^\s*([0-9.]+)\s*([KMG]iB)\s*$", mem_token)
    if not m:
        return math.nan
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "KiB":
        return val / 1024.0
    if unit == "MiB":
        return val
    if unit == "GiB":
        return val * 1024.0
    return math.nan

def parse_stats(path: Path):
    cpu_vals = []
    mem_vals = []
    if not path.exists():
        return {"samples": 0, "cpu_avg": math.nan, "cpu_max": math.nan, "mem_avg_mib": math.nan, "mem_max_mib": math.nan}
    for line in path.read_text().splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        cpu = parts[1].strip().rstrip("%")
        mem_left = parts[2].split("/")[0].strip()
        try:
            cpu_vals.append(float(cpu))
        except ValueError:
            pass
        mem = parse_mem_to_mib(mem_left)
        if not math.isnan(mem):
            mem_vals.append(mem)
    def avg(vals):
        return sum(vals) / len(vals) if vals else math.nan
    return {
        "samples": len(cpu_vals),
        "cpu_avg": avg(cpu_vals),
        "cpu_max": max(cpu_vals) if cpu_vals else math.nan,
        "mem_avg_mib": avg(mem_vals),
        "mem_max_mib": max(mem_vals) if mem_vals else math.nan,
    }

rows = []
for label in labels:
    meta = parse_meta(outdir / f"{label}.meta")
    stats = parse_stats(outdir / f"{label}.stats")
    row = {
        "stage": label,
        "elapsed_seconds": meta["elapsed_seconds"],
        "cpu_avg_percent": stats["cpu_avg"],
        "cpu_peak_percent": stats["cpu_max"],
        "mem_avg_mib": stats["mem_avg_mib"],
        "mem_peak_mib": stats["mem_max_mib"],
        "samples": stats["samples"],
        "exit_code": meta["exit_code"],
    }
    rows.append(row)

summary_path = outdir / "summary.json"
summary_path.write_text(json.dumps(rows, indent=2))

def fmt(x, digits=2):
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        if math.isnan(x):
            return "n/a"
        return f"{x:.{digits}f}"
    return str(x)

md = []
md.append("# ETL Benchmark Report")
md.append("")
md.append("This report compares legacy and modern ETL stage runs with sampled container CPU and memory usage.")
md.append("")
md.append("| Stage | Elapsed (s) | CPU Avg % | CPU Peak % | RAM Avg (MiB) | RAM Peak (MiB) | Samples | Exit |")
md.append("|---|---:|---:|---:|---:|---:|---:|---:|")
for r in rows:
    md.append(
        f"| {r['stage']} | {fmt(r['elapsed_seconds'])} | {fmt(r['cpu_avg_percent'])} | {fmt(r['cpu_peak_percent'])} | {fmt(r['mem_avg_mib'])} | {fmt(r['mem_peak_mib'])} | {fmt(r['samples'])} | {fmt(r['exit_code'])} |"
    )

legacy_total = sum(r["elapsed_seconds"] for r in rows if r["stage"].startswith("legacy_"))
modern_total = sum(r["elapsed_seconds"] for r in rows if r["stage"].startswith("modern_"))
md.append("")
md.append(f"- Legacy staged total: **{legacy_total}s**")
md.append(f"- Modern staged total: **{modern_total}s**")
if modern_total > 0:
    delta = legacy_total - modern_total
    pct = (delta / legacy_total * 100.0) if legacy_total else 0.0
    faster = "modern" if delta > 0 else "legacy"
    md.append(f"- Faster by staged total: **{faster}** ({abs(delta)}s, {abs(pct):.2f}%)")

report_path = outdir / "report.md"
report_path.write_text("\n".join(md) + "\n")
print(str(report_path))
PY
