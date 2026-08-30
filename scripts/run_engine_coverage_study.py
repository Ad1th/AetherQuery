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

# TPC-DS store_sales fact (snowflake schema, real-ish skew, signed profit
# column, ~4% NULL foreign keys). A second standard benchmark.
QUERIES_TPCDS = {
    "count_star": "SELECT COUNT(*) AS cnt FROM store_sales",
    "sum_ungrouped": "SELECT SUM(ss_sales_price) AS s FROM store_sales",
    "sum_grouped": (
        "SELECT ss_store_sk, SUM(ss_sales_price) AS s FROM store_sales "
        "WHERE ss_store_sk IS NOT NULL GROUP BY ss_store_sk"
    ),
    "sum_signed_grouped": (
        "SELECT ss_store_sk, SUM(ss_net_profit) AS p FROM store_sales "
        "WHERE ss_store_sk IS NOT NULL GROUP BY ss_store_sk"
    ),
    "avg_grouped": (
        "SELECT ss_store_sk, AVG(ss_quantity) AS q FROM store_sales "
        "WHERE ss_store_sk IS NOT NULL GROUP BY ss_store_sk"
    ),
    "sum_filtered": (
        "SELECT ss_store_sk, SUM(ss_sales_price) AS s FROM store_sales "
        "WHERE ss_store_sk IS NOT NULL AND ss_quantity > 50 GROUP BY ss_store_sk"
    ),
    "multi_agg": (
        "SELECT ss_store_sk, COUNT(*) AS c, SUM(ss_sales_price) AS s, "
        "AVG(ss_quantity) AS q FROM store_sales WHERE ss_store_sk IS NOT NULL "
        "GROUP BY ss_store_sk"
    ),
    "join_sum_by_category": (
        "SELECT i.i_category, SUM(ss.ss_sales_price) AS s "
        "FROM store_sales ss JOIN item i ON ss.ss_item_sk = i.i_item_sk "
        "WHERE i.i_category IS NOT NULL GROUP BY i.i_category"
    ),
    "join_count_by_state": (
        "SELECT s.s_state, COUNT(*) AS c "
        "FROM store_sales ss JOIN store s ON ss.ss_store_sk = s.s_store_sk "
        "GROUP BY s.s_state"
    ),
}

QUERY_SETS = {"tpch": QUERIES, "tpcds": QUERIES_TPCDS}
TARGETS = [None, 95.0, 99.0]  # None -> mode default error budget
# For --coverage-sweep: hold ε (accuracy target) fixed, vary the requested
# coverage level, and check empirical coverage tracks it.
COVERAGE_SWEEP = [0.80, 0.90, 0.95, 0.99]


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


_EXACT_FALLBACKS = {"progression_exhausted", "full_scan", "census"}


def _measure(run_approx, parse, con, qname, sql, trials, target, cov_level,
             multiplicity, anytime_valid):
    parsed = parse(sql)
    truth = _truth(con, sql, parsed)
    t0 = time.perf_counter()
    con.execute(sql).fetchall()
    exact_ms = (time.perf_counter() - t0) * 1000

    covered = total = raw_trials = exact_trials = 0
    rel_errs, signed_errs, half_widths, lat_ms, rates, raw_rel_errs = [], [], [], [], [], []
    stop_reasons = {}
    for _ in range(trials):
        t0 = time.perf_counter()
        payload = run_approx(sql, "duckdb", mode="balanced", accuracy_target=target,
                             ci_multiplicity_correction=multiplicity,
                             ci_anytime_valid=anytime_valid,
                             ci_coverage_level=cov_level)
        lat_ms.append((time.perf_counter() - t0) * 1000)
        rates.append(payload.get("sample_rate"))
        sr = payload.get("stop_reason")
        stop_reasons[sr] = stop_reasons.get(sr, 0) + 1
        is_exact = sr in _EXACT_FALLBACKS or payload.get("sample_rate", 0) >= 1.0
        raw_trials += 1
        if is_exact:
            exact_trials += 1
        for (gk, alias), (est, rel_hw) in _approx_cells(payload, parsed).items():
            tv = truth.get(gk, {}).get(alias)
            if tv is None or tv == 0:
                continue
            rel_err = abs(est - tv) / abs(tv)
            raw_rel_errs.append(rel_err)
            if is_exact:
                continue
            rel_errs.append(rel_err)
            signed_errs.append((est - tv) / abs(tv))
            if rel_hw is not None:
                half_widths.append(rel_hw)
                total += 1
                if rel_err <= rel_hw:
                    covered += 1

    _errs = rel_errs or raw_rel_errs
    return {
        "query": qname, "sql": sql.strip(), "target": target,
        "requested_coverage_level": cov_level,
        "multiplicity_correction": multiplicity, "anytime_valid": anytime_valid,
        "trials": trials,
        "empirical_coverage_pct": (100.0 * covered / total) if total else float("nan"),
        "ci_cells_scored": total,
        "exact_fallback_pct": 100.0 * exact_trials / max(1, raw_trials),
        "rel_err_p50_pct": statistics.median(_errs) * 100 if _errs else float("nan"),
        "rel_err_p95_pct": (
            statistics.quantiles(_errs, n=20)[-1] * 100
            if len(_errs) >= 20 else max(_errs) * 100 if _errs else float("nan")),
        "mean_signed_rel_error_pct": (
            statistics.mean(signed_errs) * 100 if signed_errs else float("nan")),
        "reported_half_width_p50_pct": (
            statistics.median(half_widths) * 100 if half_widths else float("nan")),
        "mean_latency_ms": statistics.mean(lat_ms),
        "exact_latency_ms": exact_ms,
        "speedup": exact_ms / statistics.mean(lat_ms) if lat_ms else float("nan"),
        "mean_sample_rate": statistics.mean(r for r in rates if r is not None),
        "stop_reasons": stop_reasons,
    }


def run(database: str, trials: int, out_path: str, multiplicity: bool = True,
        anytime_valid: bool = True, queryset: str = "tpch",
        coverage_sweep: bool = False):
    os.environ["AETHERQUERY_DUCKDB_PATH"] = str(Path(database).resolve())
    from backend.core.parser import parse_analytical_query
    from backend.core.approx_engine import run_approx
    from backend.db import duckdb as ddb

    if not multiplicity:
        print("### ABLATION: Bonferroni multiplicity correction DISABLED\n")
    if not anytime_valid:
        print("### ABLATION: anytime-valid stopping DISABLED\n")
    if coverage_sweep:
        print("### COVERAGE-LEVEL SWEEP: hold eps at 5%, vary requested coverage; "
              "empirical should track nominal\n")

    con = ddb.get_connection()
    records = []
    queries = QUERY_SETS[queryset]

    print(f"{'query':18} {'tgt/cov':9} {'cover%':7} {'n_ci':6} {'exact%':7} "
          f"{'bias%':8} {'errP95':8} {'hwP50':8} {'speedup':8}")
    print("-" * 96)

    conditions = (
        [(5.0, c) for c in COVERAGE_SWEEP] if coverage_sweep
        else [(t, 0.95) for t in TARGETS]
    )
    for qname, sql in queries.items():
        try:
            _truth(con, sql, parse_analytical_query(sql))
        except Exception as exc:
            print(f"{qname:18} SKIPPED  ({type(exc).__name__}: {str(exc)[:46]})")
            continue
        for target, cov in conditions:
            try:
                rec = _measure(run_approx, parse_analytical_query, con, qname, sql,
                               trials, target, cov, multiplicity, anytime_valid)
            except Exception as exc:
                print(f"{qname:18} FAILED ({type(exc).__name__}: {str(exc)[:46]})")
                continue
            records.append(rec)
            label = f"{cov:.2f}" if coverage_sweep else str(target)
            print(f"{qname:18} {label:9} {rec['empirical_coverage_pct']:7.1f} "
                  f"{rec['ci_cells_scored']:6d} {rec['exact_fallback_pct']:7.0f} "
                  f"{rec['mean_signed_rel_error_pct']:8.3f} {rec['rel_err_p95_pct']:8.3f} "
                  f"{rec['reported_half_width_p50_pct']:8.3f} {rec['speedup']:8.2f}")

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
    ap.add_argument("--output", default="aqp_eval/results/engine_coverage_study_sf1.json")
    ap.add_argument("--no-multiplicity-correction", action="store_true",
                    help="ablation: single-cell CI, no Bonferroni across the grid")
    ap.add_argument("--fixed-look-ci", action="store_true",
                    help="ablation: fixed 95% single-look interval, no anytime-valid schedule")
    ap.add_argument("--queryset", choices=["tpch", "tpcds"], default="tpch")
    ap.add_argument("--coverage-sweep", action="store_true",
                    help="hold eps at 5%%, vary requested coverage 0.80..0.99")
    args = ap.parse_args()
    run(args.database, args.trials, args.output,
        multiplicity=not args.no_multiplicity_correction,
        anytime_valid=not args.fixed_look_ci,
        queryset=args.queryset, coverage_sweep=args.coverage_sweep)
