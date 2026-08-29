import argparse
import json
import time
from pathlib import Path

from aqp_eval.policies import (
    ExactPolicy,
    AetherQueryPolicy,
    FixedFractionPolicy,
    GeometricPolicy,
    FixedLadderPolicy,
    OnlineAggPolicy,
)
from aqp_eval.metrics.compare import compare_results


DATABASE = "aqp_eval/datasets/tpch_sf10.duckdb"
OUTPUT = "aqp_eval/results/sf10_smoke.json"
DATASET_LABEL = "tpch_sf10"

QUERIES = {
    "Q01": "SELECT COUNT(*) AS cnt FROM lineitem",

    "Q02": """
        SELECT SUM(l_extendedprice) AS total_price
        FROM lineitem
    """,

    "Q05": """
        SELECT l_returnflag, COUNT(*) AS cnt
        FROM lineitem
        GROUP BY l_returnflag
    """,

    "Q06": """
        SELECT l_returnflag, SUM(l_extendedprice) AS total_price
        FROM lineitem
        GROUP BY l_returnflag
    """,

    "Q09": """
        SELECT c.c_mktsegment, COUNT(*) AS cnt
        FROM customer c
        JOIN orders o
            ON c.c_custkey = o.o_custkey
        GROUP BY c.c_mktsegment
    """,
}


def run(database: str = DATABASE, out_path: str = OUTPUT, dataset_label: str = DATASET_LABEL):
    policies = [
        ExactPolicy(),
        FixedFractionPolicy(fraction=0.05),
        GeometricPolicy(),
        FixedLadderPolicy(),
        OnlineAggPolicy(),
        AetherQueryPolicy(),
    ]

    results = []

    for query_id, query in QUERIES.items():

        print(f"\n=== {query_id} ===")

        exact = None
        try:
            exact = ExactPolicy().run(query, database, seed=42)
        except Exception as exc:
            print(f"Exact baseline failed: {type(exc).__name__}: {exc}")

        for policy in policies:

            print(f"Running {policy.name}...")

            start = time.perf_counter()

            failure = None
            try:
                output = policy.run(
                    query,
                    database,
                    target=0.05,
                    seed=42,
                )
            except Exception as exc:
                failure = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
                output = {}

            runner_time = (
                time.perf_counter() - start
            ) * 1000

            if failure is not None:
                error = None
            elif policy.name == "exact":
                error = 0.0
            else:
                group_keys = 1 if "GROUP BY" in query.upper() else 0

                error = compare_results(
                    exact["rows"] if exact else [],
                    output["rows"],
                    group_key_columns=group_keys,
                )

            record = {
                "dataset": dataset_label,
                "engine": "duckdb",
                "query": query_id,
                "policy": policy.name,
                "target": 0.05,
                "seed": 42,
                "trial": 1,
                "latency_ms": output.get("latency_ms"),
                "runner_time_ms": runner_time,
                "error": error,
                "iterations": output.get("iterations", 0),
                "stop_reason": output.get("stop_reason"),
                "sample_rate": output.get("sample_rate"),
                "status": "failed" if failure else "ok",
                "failure": failure,
            }

            results.append(record)

            if output.get("latency_ms") is not None:
                print(f"  latency={output['latency_ms']:.2f} ms")
            if failure is not None:
                print(f"  failed={failure['type']}: {failure['message']}")

            if error is not None:
                print(
                    f"  error={error * 100:.4f}%"
                )

    output_path = Path(out_path)
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path.write_text(
        json.dumps(results, indent=2),
        encoding="utf-8",
    )

    print(
        f"\nResults written to: {out_path}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AQP policy smoke harness")
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--output", default=OUTPUT)
    parser.add_argument("--dataset-label", default=DATASET_LABEL)
    args = parser.parse_args()
    run(args.database, args.output, args.dataset_label)
