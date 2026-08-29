"""
End-to-end coverage study for the AetherQuery adaptive engine.

The estimator library (backend.stats) has its own coverage tests. This script
exercises the *whole engine* -- parser, sampled execution, CI-based stopping --
and answers the question a Q1 reviewer asks: when the engine stops and reports a
95% interval of relative half-width h, does the true answer actually fall inside
that interval about 95% of the time, and what did that cost versus exact?

Usage:
    python scripts/run_engine_coverage_study.py \
        --database aqp_eval/datasets/tpch_sf10.duckdb --trials 100

Output: one row per (query, target) with empirical coverage, error quantiles,
and speedup, plus a JSON dump under aqp_eval/results/.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

QUERIES = {
    "count_star": "SELECT COUNT(*) AS cnt FROM lineitem",
    "sum_ungrouped": "SELECT SUM(l_extendedprice) AS s FROM lineitem",
    "sum_grouped": (
        "SELECT l_returnflag, SUM(l_extendedprice) AS s FROM lineitem "
        "GROUP BY l_returnflag"
    ),
    "avg_grouped": (
        "SELECT l_returnflag, AVG(l_extendedprice) AS a FROM lineitem "
        "GROUP BY l_returnflag"
    ),
    "sum_filtered": (
        "SELECT l_returnflag, SUM(l_extendedprice) AS s FROM lineitem "
        "WHERE l_quantity > 25 GROUP BY l_returnflag"
    ),
    "multi_agg": (
        "SELECT l_returnflag, COUNT(*) AS c, SUM(l_extendedprice) AS s, "
        "AVG(l_discount) AS a FROM lineitem GROUP BY l_returnflag"
    ),
    # INNER fact -> dimension joins: the cluster-estimator path.
    "join_count_1toN": (
        "SELECT c.c_mktsegment, COUNT(*) AS c "
        "FROM customer c JOIN orders o ON c.c_custkey = o.o_custkey "
        "GROUP BY c.c_mktsegment"
    ),
    "join_sum_star": (
        "SELECT n.n_name, SUM(l.l_extendedprice) AS s "
        "FROM lineitem l JOIN orders o ON l.l_orderkey = o.o_orderkey "
        "JOIN customer c ON o.o_custkey = c.c_custkey "
        "JOIN nation n ON c.c_nationkey = n.n_nationkey GROUP BY n.n_name"
    ),
    "join_count_fact": (
        "SELECT l.l_shipmode, COUNT(*) AS c "
        "FROM lineitem l JOIN orders o ON l.l_orderkey = o.o_orderkey "
        "GROUP BY l.l_shipmode"
    ),
}
TARGETS = [None, 95.0, 99.0]  # None -> mode default error budget


def _agg_aliases(parsed):
    return [a.alias for a in parsed.aggregates]


def _truth(con, sql, parsed):
    aliases = _agg_aliases(parsed)
    n_grp = len(parsed.group_by)
    out = {}
    for row in con.execute(sql).fetchall():
        key = tuple(row[:n_grp])
        out[key] = {aliases[i]: float(row[n_grp + i]) for i in range(len(aliases))}
    return out


def _approx_cells(payload, parsed):
    aliases = set(_agg_aliases(parsed))
    cells = {}
    ci = payload.get("ci") or {}
    hw_by_cell = {}
    for e in ci.get("estimates", []):
        gk = tuple(e["group_key"]) if isinstance(e.get("group_key"), list) else ()
        hw_by_cell[(gk, e["alias"])] = e.get("relative_half_width")
    for key, row in payload["result_map"].items():
        gk = tuple(v for k, v in row.items() if k not in aliases)
        for alias in aliases:
            if alias in row and row[alias] is not None:
                cells[(gk, alias)] = (float(row[alias]), hw_by_cell.get((gk, alias)))
    return cells


def run(database: str, trials: int, out_path: str):
    os.environ["AETHERQUERY_DUCKDB_PATH"] = str(Path(database).resolve())
    from backend.core.parser import parse_analytical_query
    from backend.core.approx_engine import run_approx
    from backend.db import duckdb as ddb

    con = ddb.get_connection()
    records = []

    print(f"{'query':16} {'target':7} {'cover%':7} {'err_p50':8} {'err_p95':8} "
          f"{'hw_p50':8} {'speedup':8} {'stop_reason':18}")
    print("-" * 90)

    for qname, sql in QUERIES.items():
        parsed = parse_analytical_query(sql)
        truth = _truth(con, sql, parsed)
        t0 = time.perf_counter()
        con.execute(sql).fetchall()
        exact_ms = (time.perf_counter() - t0) * 1000

        for target in TARGETS:
            covered = total = 0
            rel_errs, half_widths, lat_ms, rates = [], [], [], []
            stop_reasons = {}
            for _ in range(trials):
                t0 = time.perf_counter()
                payload = run_approx(sql, "duckdb", mode="balanced", accuracy_target=target)
                lat_ms.append((time.perf_counter() - t0) * 1000)
                rates.append(payload.get("sample_rate"))
                stop_reasons[payload.get("stop_reason")] = (
                    stop_reasons.get(payload.get("stop_reason"), 0) + 1
                )
                for (gk, alias), (est, rel_hw) in _approx_cells(payload, parsed).items():
                    tv = truth.get(gk, {}).get(alias)
                    if tv is None or tv == 0:
                        continue
                    rel_err = abs(est - tv) / abs(tv)
                    rel_errs.append(rel_err)
                    if rel_hw is not None:
                        half_widths.append(rel_hw)
                        total += 1
                        if rel_err <= rel_hw:
                            covered += 1

            cover_pct = (100.0 * covered / total) if total else float("nan")
            err_p50 = statistics.median(rel_errs) * 100 if rel_errs else float("nan")
            err_p95 = (
                statistics.quantiles(rel_errs, n=20)[-1] * 100
                if len(rel_errs) >= 20 else max(rel_errs) * 100 if rel_errs else float("nan")
            )
            hw_p50 = statistics.median(half_widths) * 100 if half_widths else float("nan")
            speedup = exact_ms / statistics.mean(lat_ms) if lat_ms else float("nan")
            top_stop = max(stop_reasons, key=stop_reasons.get)

            print(f"{qname:16} {str(target):7} {cover_pct:7.1f} {err_p50:8.3f} "
                  f"{err_p95:8.3f} {hw_p50:8.3f} {speedup:8.2f} {top_stop:18}")
            records.append({
                "query": qname, "sql": sql.strip(), "target": target,
                "trials": trials, "empirical_coverage_pct": cover_pct,
                "rel_err_p50_pct": err_p50, "rel_err_p95_pct": err_p95,
                "reported_half_width_p50_pct": hw_p50,
                "mean_latency_ms": statistics.mean(lat_ms),
                "exact_latency_ms": exact_ms, "speedup": speedup,
                "mean_sample_rate": statistics.mean(r for r in rates if r is not None),
                "stop_reasons": stop_reasons,
            })

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"\nWritten: {out_path}")
    print("Interpretation: empirical_coverage_pct should be >= ~93 for a nominal "
          "95% interval; values well below that mean the CLT interval is "
          "under-covering (skew) and a finite-sample method is needed.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", default="aqp_eval/datasets/tpch_sf10.duckdb")
    ap.add_argument("--trials", type=int, default=100)
    ap.add_argument("--output", default="aqp_eval/results/engine_coverage_study.json")
    args = ap.parse_args()
    run(args.database, args.trials, args.output)
