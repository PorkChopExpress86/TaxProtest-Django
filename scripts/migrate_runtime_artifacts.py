#!/usr/bin/env python3
from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_PATHS_FILE = BASE_DIR / "taxprotest" / "runtime_paths.py"
spec = spec_from_file_location("runtime_paths", RUNTIME_PATHS_FILE)
runtime_paths = module_from_spec(spec)
assert spec is not None and spec.loader is not None
sys.modules[spec.name] = runtime_paths
spec.loader.exec_module(runtime_paths)


def main() -> int:
    result = runtime_paths.migrate_runtime_artifacts(BASE_DIR)
    paths = runtime_paths.resolve_runtime_paths(BASE_DIR)

    print("Runtime artifact migration complete.")
    print(f"  downloads: {paths.download_dir}")
    print(f"  extracted: {paths.extract_dir}")
    print(f"  logs:      {paths.log_dir}")
    print(f"  reports:   {paths.report_dir}")

    if result["created"]:
        print("  created:")
        for item in result["created"]:
            print(f"    - {item}")

    if result["moved"]:
        print("  moved:")
        for item in result["moved"]:
            print(f"    - {item}")
    else:
        print("  moved: none")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
