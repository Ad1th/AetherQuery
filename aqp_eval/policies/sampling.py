from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from aqp_eval.policies.base import Policy

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def run_sample(query, database, fraction, seed=42):
    os.environ["AETHERQUERY_DUCKDB_PATH"] = str(
        Path(database).resolve()
    )

    from backend.core.parser import parse_analytical_query
    from backend.core.executor import fetch_aggregated_sample
    from backend.core.join_sampling import execute_stratified_join_sample

    parsed = parse_analytical_query(query)

    start = time.perf_counter()

    if parsed.has_joins:
        payload, query_time, sample_query = execute_stratified_join_sample(
            parsed,
            "duckdb",
            fraction,
        )
    else:
        payload, query_time, sample_query = fetch_aggregated_sample(
            parsed,
            "duckdb",
            fraction,
        )

    elapsed = time.perf_counter() - start

    return {
        "rows": payload["rows"],
        "columns": payload["columns"],
        "latency_ms": elapsed * 1000,
        "sample_rate": fraction,
        "sample_query": sample_query,
        "query_time_ms": query_time * 1000,
        "seed": seed,
    }


class FixedFractionPolicy(Policy):
    name = "fixed_fraction"

    def __init__(self, fraction=0.05):
        self.fraction = fraction

    def run(
        self,
        query,
        database,
        target=None,
        seed=42,
    ):
        result = run_sample(
            query,
            database,
            self.fraction,
            seed,
        )

        return {
            **result,
            "policy": self.name,
            "iterations": 1,
            "stop_reason": "fixed_fraction",
            "target": target,
            "error": None,
        }
