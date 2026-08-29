import os
import time
from pathlib import Path
import sys

from aqp_eval.policies.base import Policy

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class AetherQueryPolicy(Policy):
    name = "aetherquery"

    def run(
        self,
        query,
        database,
        target=None,
        seed=42,
    ):
        os.environ["AETHERQUERY_DUCKDB_PATH"] = str(
            Path(database).resolve()
        )

        from backend.core.approx_engine import run_approx

        start = time.perf_counter()

        payload = run_approx(
            query=query,
            source="duckdb",
            mode="balanced",
            accuracy_target=target,
        )

        elapsed = time.perf_counter() - start

        return {
            "policy": self.name,
            "rows": payload.get(
                "rows",
                payload.get("result", []),
            ),
            "columns": payload.get("columns", []),
            "latency_ms": elapsed * 1000,
            "iterations": len(
                payload.get("iterations", [])
            ),
            "error": None,
            "stop_reason": payload.get("stop_reason"),
            "seed": seed,
            "target": target,
            "sample_rate": payload.get("sample_rate"),
        }
