"""
Does the design-effect constant transfer to a physically clustered table?

The constant D in the single-table variance was calibrated on TPC-H and TPC-DS,
whose rows are generated in an order unrelated to the columns a query groups or
aggregates. A real table is usually stored in the order it arrived, so a block
of 2048 physically adjacent rows is a *time slice*, and any column that varies
with time is correlated within a block. That is precisely the condition under
which TABLESAMPLE SYSTEM's design effect grows.

This script measures, on any table, three things per query:

  1. the realized design effect of SYSTEM block sampling, measured from block
     aggregates as Var_cluster / Var_srs on the same rows;
  2. empirical coverage of the engine's interval under SYSTEM sampling at a
     fixed fraction, swept over candidate values of the constant D;
  3. empirical coverage of the same interval under BERNOULLI (row-level)
     sampling with D = 1, which is the design the variance formula actually
     assumes. If (3) covers where (2) does not, the failure is the block
     design effect and not the estimator.
  4. empirical coverage of a single-stage *cluster* interval over the same
     SYSTEM sample, taking the physical row group as the sampling unit and the
     between-block variance as the dispersion. This is the construction the
     join path already uses, applied to a single table; it needs no constant
     and keeps SYSTEM's block skipping.

Usage:
    python scripts/run_physical_clustering_study.py \
        --database aqp_eval/datasets/nyctaxi.duckdb --queryset taxi \
        --trials 200 --output aqp_eval/results/physical_clustering.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from run_engine_coverage_study import QUERY_SETS, _agg_aliases, _truth  # noqa: E402

DEFFS = [1.0, 1.75, 3.0, 5.0, 8.0, 12.0]
DUCKDB_VECTOR_SIZE = 2048


def _cells(estimate_set, parsed):
    out = {}
    for group_key, by_alias in estimate_set.estimates.items():
        gk = tuple(group_key) if isinstance(group_key, tuple) else ()
        for alias, est in by_alias.items():
            if est.estimate is not None:
                out[(gk, alias)] = (float(est.estimate), est.relative_half_width)
    return out


def _cluster_cells(ss, parsed, fraction, population, coverage_level=0.95):
    """
    Single-stage cluster interval for a single-table query over one SYSTEM
    sample: group the sampled rows by physical row group as well as by the
    query's key, form per-(block, group) totals, and take the between-block
    variance. Absent blocks contribute genuine zeros, so the denominator is the
    number of sampled blocks, not the number in which the group appeared.
    """
    import math as _m
    from backend.stats.contracts import Aggregate, Correction

    where = parsed.where_clause
    filt = f" FILTER (WHERE {where})" if where else ""
    sel = [f"(rowid // {DUCKDB_VECTOR_SIZE}) AS __blk"]
    sel += [f"{g} AS __g{i}" for i, g in enumerate(parsed.group_by)]
    sel.append("COUNT(*) AS __n")
    sel.append(f"COUNT(*){filt} AS __nd")
    aggs = []
    for a in parsed.aggregates:
        fn = a.func.lower()
        if fn == "count":
            aggs.append((a, "count"))
            continue
        col = ss._col_expr(a.expression)
        sel.append(f"SUM({col}){filt} AS __t_{a.alias}")
        aggs.append((a, fn))
    grp = ["__blk"] + [f"__g{i}" for i in range(len(parsed.group_by))]
    sql = (f"SELECT {', '.join(sel)} FROM {parsed.table} "
           f"TABLESAMPLE SYSTEM ({fraction * 100.0:.4f} PERCENT) "
           f"GROUP BY {', '.join(grp)}")
    payload = ss._execute_source_query(sql, "duckdb")
    cols = payload.get("columns", [])
    rows = [dict(zip(cols, r)) for r in payload.get("rows", [])]
    if not rows:
        return {}

    total_blocks = max(1, _m.ceil(population / DUCKDB_VECTOR_SIZE))
    m_total = len({r["__blk"] for r in rows})
    n_sample = sum(int(r.get("__n") or 0) for r in rows)
    ngrp = len(parsed.group_by)

    per_group = {}
    for r in rows:
        gk = tuple(r.get(f"__g{i}") for i in range(ngrp))
        per_group.setdefault(gk, []).append(r)

    k = max(1, len(per_group) * len(parsed.aggregates))
    per_interval = 1.0 - (1.0 - coverage_level) / k

    out = {}
    for gk, brows in per_group.items():
        n_domain = sum(int(r.get("__nd") or 0) for r in brows)
        for a, fn in aggs:
            if fn == "count":
                totals = [float(r.get("__nd") or 0.0) for r in brows]
                agg = Aggregate.COUNT
            else:
                totals = [float(r.get(f"__t_{a.alias}") or 0.0) for r in brows]
                agg = Aggregate.SUM
            if fn == "avg":
                # ratio of two cluster totals: linearise on the residual
                num = sum(totals)
                den = sum(float(r.get("__nd") or 0.0) for r in brows)
                if den == 0:
                    continue
                ratio = num / den
                res = [t - ratio * float(r.get("__nd") or 0.0)
                       for t, r in zip(totals, brows)]
                sum_t, sum_tt = sum(res), sum(v * v for v in res)
                est = ss._cluster_interval_from_block_totals(
                    Aggregate.SUM, sum_t, sum_tt, m_total, total_blocks,
                    per_interval, n_sample, n_domain)
                hw = est.half_width
                den_hat = den / (m_total / total_blocks)
                rel = (hw / (den_hat * abs(ratio))) if (hw and ratio) else None
                out[(gk, a.alias)] = (ratio, rel)
                continue
            sum_t, sum_tt = sum(totals), sum(t * t for t in totals)
            est = ss._cluster_interval_from_block_totals(
                agg, sum_t, sum_tt, m_total, total_blocks, per_interval,
                n_sample, n_domain)
            if est.estimate is not None:
                out[(gk, a.alias)] = (float(est.estimate), est.relative_half_width)
    return out


def _score(cells, truth):
    n = cov = 0
    widths = []
    for (gk, alias), (est, rhw) in cells.items():
        tv = truth.get(gk, {}).get(alias)
        if tv is None or tv == 0 or rhw is None:
            continue
        n += 1
        widths.append(rhw)
        if abs(est - tv) / abs(tv) <= rhw:
            cov += 1
    return n, cov, widths


def run(database, queryset, queries, trials, fraction, out_path):
    os.environ["AETHERQUERY_DUCKDB_PATH"] = str(Path(database).resolve())
    from backend.core.parser import parse_analytical_query
    from backend.core import sufficient_stats as ss
    from backend.db import duckdb as ddb

    con = ddb.get_connection()
    records = []
    # Measure the design effect without the engine's reporting clamp, so the
    # paper can say how far above the deployed constant it actually sits.
    original_clamp = ss.MAX_MEASURED_DESIGN_EFFECT
    ss.MAX_MEASURED_DESIGN_EFFECT = 1e6

    print(f"{'query':24} {'design':10} {'D':>6} {'cov%':>7} {'n':>7} {'hw50%':>8}")
    print("-" * 68)

    for qname in queries:
        sql = QUERY_SETS[queryset][qname]
        try:
            parsed = parse_analytical_query(sql)
            truth = _truth(con, sql, parsed)
        except Exception as exc:
            print(f"{qname:24} SKIPPED ({type(exc).__name__})")
            continue

        # (1) measured design effect of SYSTEM sampling on this query
        os.environ["AETHERQUERY_TABLESAMPLE"] = "SYSTEM"
        measured = []
        for _ in range(15):
            try:
                d = ss._measure_system_design_effect(parsed, "duckdb", fraction)
            except Exception:
                d = None
            if d is not None:
                measured.append(d)
        deff_p50 = statistics.median(measured) if measured else None
        deff_max = max(measured) if measured else None

        # (4) cluster interval over a SYSTEM sample: no constant, keeps
        # block skipping. Measurement only; not on the engine's default path.
        os.environ["AETHERQUERY_TABLESAMPLE"] = "SYSTEM"
        population = ss._population_size(parsed.table, "duckdb")
        n_tot = cov_tot = 0
        widths = []
        for _ in range(trials):
            try:
                cells = _cluster_cells(ss, parsed, fraction, population)
            except Exception:
                continue
            n, cov, w = _score(cells, truth)
            n_tot += n
            cov_tot += cov
            widths.extend(w)
        if n_tot:
            rec = {
                "query": qname, "sql": sql.strip(),
                "design": "SYSTEM+cluster", "design_effect": None,
                "sample_fraction": fraction, "trials": trials,
                "empirical_coverage_pct": 100.0 * cov_tot / n_tot,
                "ci_cells_scored": n_tot,
                "reported_half_width_p50_pct":
                    statistics.median(widths) * 100 if widths else float("nan"),
                "measured_system_deff_p50": deff_p50,
                "measured_system_deff_max": deff_max,
            }
            records.append(rec)
            print(f"{qname:24} {'SYS+clust':10} {'  n/a':>6} "
                  f"{rec['empirical_coverage_pct']:7.1f} {n_tot:7d} "
                  f"{rec['reported_half_width_p50_pct']:8.3f}")

        for design in ("SYSTEM", "BERNOULLI"):
            # The engine's own opt-in switch, so this measures the shipped
            # code path rather than a harness-local rewrite.
            os.environ["AETHERQUERY_TABLESAMPLE"] = design
            deffs = DEFFS if design == "SYSTEM" else [1.0]
            for D in deffs:
                n_tot = cov_tot = 0
                widths = []
                for _ in range(trials):
                    try:
                        es, _met, _detail = ss.evaluate_sample_accuracy(
                            parsed, "duckdb", fraction,
                            coverage_level=0.95, target_relative_error=0.05,
                            design_effect=D, multiplicity_correction=True)
                    except Exception:
                        continue
                    n, cov, w = _score(_cells(es, parsed), truth)
                    n_tot += n
                    cov_tot += cov
                    widths.extend(w)
                if n_tot == 0:
                    continue
                rec = {
                    "query": qname, "sql": sql.strip(), "design": design,
                    "design_effect": D, "sample_fraction": fraction,
                    "trials": trials,
                    "empirical_coverage_pct": 100.0 * cov_tot / n_tot,
                    "ci_cells_scored": n_tot,
                    "reported_half_width_p50_pct":
                        statistics.median(widths) * 100 if widths else float("nan"),
                    "measured_system_deff_p50": deff_p50,
                    "measured_system_deff_max": deff_max,
                }
                records.append(rec)
                print(f"{qname:24} {design:10} {D:6.2f} "
                      f"{rec['empirical_coverage_pct']:7.1f} {n_tot:7d} "
                      f"{rec['reported_half_width_p50_pct']:8.3f}")
        os.environ["AETHERQUERY_TABLESAMPLE"] = "SYSTEM"

        if deff_p50 is not None:
            print(f"{qname:24} measured SYSTEM DEFF: median {deff_p50:.2f}, "
                  f"max {deff_max:.2f}")

    try:
        sha = subprocess.check_output(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        sha = "unknown"
    try:
        import duckdb as _dk
        dv = _dk.__version__
    except Exception:
        dv = "unknown"

    payload = {
        "provenance": {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "engine_git_sha": sha, "duckdb_version": dv,
            "python_version": platform.python_version(),
            "host": platform.platform(),
            "database": str(Path(database).resolve()),
            "queryset": queryset, "trials": trials,
            "sample_fraction": fraction,
            "design_effects_swept": DEFFS,
            "experiment": "physical_clustering_and_design_effect_transfer",
            "description": (
                "Fixed-fraction coverage under TABLESAMPLE SYSTEM across "
                "candidate design-effect constants, against the same interval "
                "under BERNOULLI row sampling with D=1, plus the measured "
                "SYSTEM design effect for each query."
            ),
        },
        "records": records,
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    ss.MAX_MEASURED_DESIGN_EFFECT = original_clamp
    os.environ.pop("AETHERQUERY_TABLESAMPLE", None)
    Path(out_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWritten: {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", required=True)
    ap.add_argument("--queryset", default="taxi",
                    choices=["tpch", "tpcds", "taxi"])
    ap.add_argument("--queries", nargs="*", default=None)
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--fraction", type=float, default=0.01)
    ap.add_argument("--output",
                    default="aqp_eval/results/physical_clustering.json")
    a = ap.parse_args()
    qs = a.queries or list(QUERY_SETS[a.queryset].keys())
    run(a.database, a.queryset, qs, a.trials, a.fraction, a.output)
