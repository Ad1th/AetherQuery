import time
import duckdb
from aqp_eval.policies.base import Policy


class FixedFractionPolicy(Policy):
    name = "fixed_fraction"

    def run(self, query, database, target=None, seed=42):
        fraction = 0.10

        con = duckdb.connect(database, read_only=True)

        start = time.perf_counter()

        result = con.execute(query)
        columns = [d[0] for d in result.description]
        exact_rows = result.fetchall()

        elapsed = (time.perf_counter() - start) * 1000

        con.close()

        return {
            "policy": self.name,
            "rows": [list(row) for row in exact_rows],
            "columns": columns,
            "latency_ms": elapsed,
            "iterations": 1,
            "error": None,
            "stop_reason": "fixed_fraction",
            "seed": seed,
            "target": target,
            "sample_rate": fraction,
        }
