# AetherQuery Handoff

**Snapshot:** 2026-08-30
**Repository:** `/Users/nehadamani/Developer/AetherQuery`
**Branch:** `master` (HEAD `d5eef92`)

## What this project is

AetherQuery is a SQL execution and approximate-query-processing platform. It exposes a FastAPI backend and a React/Vite frontend for:

- Exact execution against DuckDB, PostgreSQL, and MySQL.
- Approximate `COUNT`, `SUM`, and `AVG` execution with sampling, grouping, filters, and JOIN support.
- Query-plan inspection and visualization.
- CSV upload into DuckDB.
- Query rewriting, caching, execution progress, history, and exact-vs-approx benchmarking.

## Current repository layout

- `backend/` — active Python/FastAPI application.
- `frontend/` — React 19 + TypeScript + Vite UI.
- `aqp_eval/` — policy-based evaluation harness, truth data, metrics, and smoke results.
- `scripts/` — TPC-H data generation, JOIN tests, and profiling utilities.
- `datasets/` — local DuckDB database and uploaded CSVs. `aqp_eval/datasets/` contains large benchmark databases.
- `backend-rust-incomplete/` — incomplete Rust backend; not the active service.
- `oldcodes/` — legacy/reference code; treat as read-only.

## How to run

From the repository root:

```bash
.venv/bin/python -m uvicorn backend.main:app --reload --port 8093
```

API docs: `http://127.0.0.1:8093/docs`

Run the frontend in another terminal:

```bash
cd frontend
npm install
npm run dev
```

The UI expects the backend at `http://127.0.0.1:8093` (configured directly in `frontend/src/App.tsx`). Routes are `/` for the executor and `/plan` for the plan analyzer.

## Main API surface

- `POST /api/execute` and `/api/sql/execute` — exact, approximate, fast, accurate, balanced, or benchmark execution.
- `GET /api/sql/execute/progress/{request_id}` — progress for a running execution.
- `POST /api/plan` and `/api/sql/parse-plan` — explain and normalize a query plan.
- `POST /api/upload` — upload a CSV and create a DuckDB table.
- `POST /api/optimize` — produce an approximate-query rewrite.
- `GET /api/history` and `/history` — recent in-memory query history.
- `POST /api/cache/clear` — clear cache and, by default, history.

DuckDB defaults to `datasets/aetherquery.duckdb`. PostgreSQL uses `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and `PGPASSWORD`; MySQL uses the corresponding `MYSQL_*` variables. See `backend/db/` for adapter behavior.

## Important implementation notes

Approximate execution is routed through `backend/core/router.py`, with the main logic in `approx_engine.py` and `runtime_sampling.py`. JOIN queries use parser metadata and stratified sampling in `join_sampling.py`; JOINs are forced through adaptive progression and receive a higher minimum sample rate. The implementation also contains semi-join reduction, join ordering, HyperLogLog, Bloom-filter, and error-bound support, but these areas still need broader validation.

The frontend supports CSV upload, side-by-side exact/approx execution, plan graphs, progress polling, query history, and an accuracy target control. The plan page is routed from `frontend/src/main.tsx`.

## Current worktree state

The worktree is not clean. Uncommitted changes exist in:

- `backend/core/executor.py`
- `backend/core/join_sampling.py`
- `backend/core/parser.py`
- `backend/tests/test_join_sampling.py`
- New, untracked `aqp_eval/` files/results

The latest committed work is focused on JOIN sampling, adaptive sampling, query parsing, and evaluation support. Preserve the current changes when branching or rebasing; they belong to the in-progress work.

## Known issues / risks

The existing TPC-H JOIN report (`APPROXIMATE_JOIN_RESULTS.md`) records that early 1% JOIN sampling frequently returned no rows and could be slower than exact execution. Recommended follow-up is to validate the newer adaptive/minimum-rate changes against the TPC-H datasets, especially selective and multi-way JOINs.

`backend/core/error_bounds.py` still contains an explicit `NotImplementedError` for runtime integration. Approximate-result accuracy must therefore be treated as experimental until the evaluation suite is rerun across several sample rates and query shapes.

Query history and cache are process-local and disappear on restart. CSV uploads write generated files into `datasets/`; clean-up and persistence policy are not yet automated.

## Verification status at handoff

- Python test command attempted: `.venv/bin/python -m pytest backend/tests aqp_eval/metrics/test_compare.py -q` — blocked because `pytest` is not installed in `.venv`.
- Frontend build attempted: `npm run build` — blocked because frontend dependencies/types are not installed (`vite/client`, `vite`, and `@vitejs/plugin-react` unavailable).
- Existing benchmark artifacts are in `aqp_eval/results/sf1_smoke.json` and `aqp_eval/results/sf10_smoke.json`; they should be regenerated after the current JOIN changes.

## Suggested next steps

1. Install dependencies (`pip install -r backend/requirements.txt` plus `pytest`; `npm install` in `frontend`).
2. Run backend unit tests and `npm run build` / `npm run lint`.
3. Re-run the TPC-H JOIN scripts and `aqp_eval.runners.run` against the available SF1/SF10 databases.
4. Compare exact and approximate rows by group, error, latency, sample rate, and stop reason; investigate empty groups and regressions.
5. Decide whether to commit the current parser/executor/JOIN changes and the `aqp_eval/` harness as one logical change set.
