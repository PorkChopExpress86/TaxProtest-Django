# Coding standards

Read during code review. These are judgement calls; mechanical rules are enforced by tests
and linters instead (for example `counties/common/tests/test_shared_code_boundaries.py`).

## Query cost

A request's query count must not grow with the size of the dataset. Where code scans
until it finds enough rows, check the sparse case, where the scan never finds enough and
reads everything. A query-count test for that case belongs beside the change.

## Behaviour contracts

Exit codes, strict-mode failures, Celery result keys and persisted evidence are contracts
(ADR-0002, ADR-0024). A refactor that reroutes how a result is classified keeps a test that
goes red if a strict run that ends incomplete stops failing, or if an exit code changes.
