import time
import duckdb

from aqp_eval.policies.base import Policy


class ExactPolicy(Policy):
    name = "exact"

    def run(
        self,
        query,
        database,
        target=None,
        seed=42,
    ):
        con = duckdb.connect(database, read_only=False)

        start = time.perf_counter()

        result = con.execute(query)

        columns = [
            description[0]
            for description in result.description
        ]

        rows = result.fetchall()

        elapsed = time.perf_counter() - start

        con.close()

        return {
            "policy": self.name,
            "rows": [list(row) for row in rows],
            "columns": columns,
            "latency_ms": elapsed * 1000,
            "iterations": 1,
            "error": 0.0,
            "stop_reason": "exact",
            "seed": seed,
            "target": target,
        }
