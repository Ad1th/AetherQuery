# AetherQuery

Unified backend for exact and approximate SQL execution, plan parsing, and plan matching.

## Structure

- backend: FastAPI backend with modular engines and APIs
- frontend: existing UI (copied from legacy AetherQuery)
- datasets: uploaded CSV files and local DuckDB file
- experiments: run artifacts
- oldcodes: read-only reference area for legacy code

## Backend Features

- Execute SQL in exact mode (DuckDB, Postgres, MySQL)
- Execute SQL in approximate mode for COUNT/SUM/AVG queries with optional `WHERE`, `GROUP BY`, multi-aggregate select lists, and runtime sampling
- Upload CSV and query it through DuckDB
- Parse query plans for visualization
- Compare plans with a structural similarity score
- Lightweight in-memory cache for repeated query requests
- Benchmark exact vs approximate execution with error and speedup metrics

## Run

1. Create and activate a virtual environment.
2. Install dependencies:

   pip install -r backend/requirements.txt

3. Start API server from project root:

   .venv/bin/python -m uvicorn backend.main:app --reload --port 8093

   If you are inside the backend directory, use:

   ../.venv/bin/python -m uvicorn main:app --reload --app-dir .. --port 8093

4. Open docs:

   http://127.0.0.1:8093/docs

## Core API

- POST /api/upload
- POST /api/execute
- POST /api/plan
- POST /api/optimize

## Approximate Execution Modes

Send `mode` in `POST /api/execute` as one of:

- `exact`
- `approx` or `balanced`
- `fast`
- `accurate`
- `benchmark`

Example request body:

```json
{
  "source": "duckdb",
  "mode": "benchmark",
  "query": "SELECT l_returnflag, SUM(l_quantity), COUNT(*) FROM lineitem GROUP BY l_returnflag ORDER BY l_returnflag"
}
```

## Notes

- DuckDB default file is at datasets/aetherquery.duckdb.
- Postgres and MySQL connections are configured via environment variables.
- If you see ModuleNotFoundError: No module named 'backend', run uvicorn from the project root (WT26) with backend.main:app.
- If you see No module named uvicorn after activating the venv, check for a shell alias: run command `alias python`. If it points to system Python, use `.venv/bin/python` explicitly.
