---
status: accepted
---

# Document layout and ETL stress verification

Visual document fidelity and high-volume ETL pipeline stability must be verified
without violating repository architecture boundaries or bloating execution environments.

1. **Document layout verification**: Evidence PDF documents must be verified using
   pure-Python structural stream assertions (page object boundaries, multi-page chunking,
   line count limits, and coordinate bounds) rather than headless browser engines or
   rasterized pixel-diff snapshots. This keeps the Docker image free of native browser
   toolchains while guaranteeing dense protest dossiers never suffer silent line overflows.

2. **ETL stress verification**: County ETL pipelines must use a two-tiered testing model:
   fast synthetic multi-stage candidate generators (thousands of records) inside CI pytest
   to guard memory bounds, chunking, and qualification logic, accompanied by an on-demand
   operator management command for benchmarking throughput against multi-gigabyte files.

3. **Deployment readiness verification**: Deployments must verify migration currency,
   database schema accessibility across all county tables, and Celery beat schedule bindings
   both as a management check command and as a post-deploy verification step.
