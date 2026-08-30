"""
Head-to-head comparison of the AetherQuery grid controller against an
online-aggregation-style per-aggregate stopping baseline.

Both arms run in the same process, over the same queries, against the same
exact truth, on the same machine, with the same number of trials, and over the
*same* sample-size ladder (both call `runtime_sampling.resolve_sampling_plan`,
so the ladder is shared by construction rather than transcribed). The arms
differ only in the stopping policy:

  aetherquery  the full controller: Bonferroni correction across the result
               grid, harmonic alpha spending across looks, a grid-completeness
               condition, a worst-cell stop, and a fallback to exact execution
               when the target cannot be certified.

  ola          an online-aggregation-style stopper in the spirit of
               Hellerstein et al. (1997): every cell carries its own running
               estimate and its own nominal 95% interval; a cell is finished
               and freezes its reported value the first time its own relative
               half-width reaches epsilon; the query finishes when every cell
               it has observed is finished. No multiplicity correction, no
               alpha spending, no completeness condition, and no fallback to
               exact -- when the ladder runs out it reports the running
               estimate and interval it has.

This is an implementation of the online-aggregation *stopping discipline*, not
a reproduction of any named published system. It is deliberately given
AetherQuery's own estimator layer (the same expansion / empirical-Bernstein
per-cell constructions, and the same cluster estimator on the certified join
class), so the measured difference is attributable to the controller and not
to estimator quality. That choice favours the baseline.

Metrics are computed identically for both arms:

  cell coverage           fraction of scored cells whose reported interval
                          contains the truth
  grid coverage           fraction of scored trials in which *every* cell of
                          the returned grid is covered simultaneously
  mis-certification rate  fraction of *all* trials that returned an
                          approximate answer with at least one uncovered cell
                          (the decision-relevant risk: an answer was handed
                          back and it was outside its own interval)

Usage:
    python scripts/run_baseline_comparison.py \
        --database aqp_eval/datasets/tpch_sf1.duckdb --trials 40 \
        --output aqp_eval/results/baseline_comparison_sf1.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from run_engine_coverage_study import (  # noqa: E402
    QUERY_SETS,
    _agg_aliases,
    _truth,
    _EXACT_FALLBACKS,
)

# Same conditions as the main coverage study.
TARGETS = [None, 95.0, 99.0]
COVERAGE_LEVEL = 0.95


# --------------------------------------------------------------------------
# arm 1: the AetherQuery controller, driven exactly as a user would drive it
# --------------------------------------------------------------------------
def _run_aetherquery(run_approx, parsed, sql, target):
    t0 = time.perf_counter()
    payload = run_approx(
        sql, "duckdb", mode="balanced", accuracy_target=target,
        ci_multiplicity_correction=True, ci_anytime_valid=True,
        ci_coverage_level=COVERAGE_LEVEL,
    )
    latency_ms = (time.perf_counter() - t0) * 1000.0

    stop_reason = payload.get("stop_reason")
    sample_rate = payload.get("sample_rate") or 0.0
    declined = stop_reason in _EXACT_FALLBACKS or sample_rate >= 1.0

    aliases = set(_agg_aliases(parsed))
    cells = {}
    hw = {}
    for e in (payload.get("ci") or {}).get("estimates", []):
        gk = tuple(e["group_key"]) if isinstance(e.get("group_key"), list) else ()
        hw[(gk, e["alias"])] = e.get("relative_half_width")
    for row in payload["result_map"].values():
        gk = tuple(v for k, v in row.items() if k not in aliases)
        for alias in aliases:
            if row.get(alias) is not None:
                cells[(gk, alias)] = (float(row[alias]), hw.get((gk, alias)))
    looks = payload.get("iterations") or []
    return {
        "cells": cells, "latency_ms": latency_ms, "declined": declined,
        "sample_rate": sample_rate, "stop_reason": stop_reason,
        "looks": len(looks),
        "fraction_read": sum(float(it.get("sample_fraction") or 0.0) for it in looks),
    }


# --------------------------------------------------------------------------
# arm 2: the online-aggregation-style per-aggregate stopper
# --------------------------------------------------------------------------
def _run_ola(evaluator, parse, sql, source, progression, eps):
    """
    Walk the same ladder. At each look form per-cell intervals at a fixed
    nominal 95% with no multiplicity correction. A cell freezes the first time
    its own relative half-width is within eps. Stop when every observed cell
    has frozen, or when the ladder is exhausted (report what is on hand).
    """
    frozen: dict[tuple, tuple[float, float | None, float]] = {}
    latest: dict[tuple, tuple[float, float | None, float]] = {}
    t0 = time.perf_counter()
    # Parse inside the timed region so both arms pay the same front-end cost.
    parsed = parse(sql)
    last_fraction = progression[-1] if progression else 1.0
    exhausted = True
    looks = 0
    fraction_read = 0.0

    for fraction in progression:
        looks += 1
        fraction_read += fraction
        last_fraction = fraction
        estimate_set, _met, _detail = evaluator(
            parsed, source, fraction,
            coverage_level=COVERAGE_LEVEL,          # fixed level at every look
            target_relative_error=eps,
            multiplicity_correction=False,          # no Bonferroni over the grid
        )
        observed = []
        for group_key, by_alias in estimate_set.estimates.items():
            gk = tuple(group_key) if isinstance(group_key, tuple) else ()
            for alias, est in by_alias.items():
                if est.estimate is None:
                    continue
                key = (gk, alias)
                observed.append(key)
                latest[key] = (float(est.estimate), est.relative_half_width, fraction)
                if key in frozen:
                    continue
                rhw = est.relative_half_width
                if rhw is not None and rhw <= eps:
                    frozen[key] = (float(est.estimate), rhw, fraction)
        if observed and all(k in frozen for k in observed):
            exhausted = False
            break

    latency_ms = (time.perf_counter() - t0) * 1000.0
    # Cells that never met the target report their running estimate and
    # interval: an online-aggregation display has no exact fallback.
    cells = dict(latest)
    cells.update({k: v for k, v in frozen.items()})
    return {
        "cells": {k: (v[0], v[1]) for k, v in cells.items()},
        "latency_ms": latency_ms,
        "declined": False,
        "sample_rate": last_fraction,
        "stop_reason": "ladder_exhausted" if exhausted else "per_cell_target_met",
        "looks": looks,
        "fraction_read": fraction_read,
    }


# --------------------------------------------------------------------------
def _score(trial, truth):
    """Return (n_scored, n_covered, all_covered, rel_errs, signed, widths)."""
    n_scored = n_covered = 0
    rel_errs, signed, widths = [], [], []
    all_covered = True
    for (gk, alias), (est, rhw) in trial["cells"].items():
        tv = truth.get(gk, {}).get(alias)
        if tv is None or tv == 0:
            continue
        rel = abs(est - tv) / abs(tv)
        rel_errs.append(rel)
        signed.append((est - tv) / abs(tv))
        if rhw is None:
            continue
        widths.append(rhw)
        n_scored += 1
        if rel <= rhw:
            n_covered += 1
        else:
            all_covered = False
    return n_scored, n_covered, all_covered, rel_errs, signed, widths


def _summarise(policy, qname, sql, target, trials, results, exact_ms):
    cells_scored = cells_covered = 0
    grids_scored = grids_covered = 0
    declined = mis_certified = 0
    rel_errs, signed, widths, lat, rates = [], [], [], [], []
    looks, fracs = [], []
    stop_reasons: dict[str, int] = {}

    for trial in results:
        lat.append(trial["latency_ms"])
        rates.append(trial["sample_rate"])
        looks.append(trial["looks"])
        fracs.append(trial["fraction_read"])
        sr = trial["stop_reason"]
        stop_reasons[sr] = stop_reasons.get(sr, 0) + 1
        if trial["declined"]:
            declined += 1
            continue
        n_s, n_c, ok, re_, sg, wd = _score(trial, trial["_truth"])
        rel_errs.extend(re_)
        signed.extend(sg)
        widths.extend(wd)
        if n_s == 0:
            continue
        cells_scored += n_s
        cells_covered += n_c
        grids_scored += 1
        grids_covered += 1 if ok else 0
        if not ok:
            mis_certified += 1

    n = max(1, len(results))
    return {
        "policy": policy, "query": qname, "sql": sql.strip(), "target": target,
        "trials": trials,
        "cell_coverage_pct": 100.0 * cells_covered / cells_scored if cells_scored else float("nan"),
        "grid_coverage_pct": 100.0 * grids_covered / grids_scored if grids_scored else float("nan"),
        "cells_scored": cells_scored,
        "grids_scored": grids_scored,
        "mis_certification_pct": 100.0 * mis_certified / n,
        "exact_fallback_pct": 100.0 * declined / n,
        "rel_err_p50_pct": statistics.median(rel_errs) * 100 if rel_errs else float("nan"),
        "rel_err_p95_pct": (
            statistics.quantiles(rel_errs, n=20)[-1] * 100 if len(rel_errs) >= 20
            else max(rel_errs) * 100 if rel_errs else float("nan")),
        "mean_signed_rel_error_pct": statistics.mean(signed) * 100 if signed else float("nan"),
        "reported_half_width_p50_pct": statistics.median(widths) * 100 if widths else float("nan"),
        "mean_latency_ms": statistics.mean(lat) if lat else float("nan"),
        "exact_latency_ms": exact_ms,
        "speedup": exact_ms / statistics.mean(lat) if lat else float("nan"),
        "mean_sample_rate": statistics.mean(r for r in rates if r is not None),
        "mean_looks": statistics.mean(looks) if looks else float("nan"),
        "mean_fraction_read": statistics.mean(fracs) if fracs else float("nan"),
        "stop_reasons": stop_reasons,
    }


def run(database: str, trials: int, out_path: str, queryset: str = "tpch"):
    os.environ["AETHERQUERY_DUCKDB_PATH"] = str(Path(database).resolve())
    from backend.core.parser import parse_analytical_query
    from backend.core.approx_engine import run_approx
    from backend.core.runtime_sampling import resolve_sampling_plan
    from backend.core.sufficient_stats import (
        evaluate_sample_accuracy, evaluate_join_sample_accuracy, join_ci_is_defensible,
    )
    from backend.db import duckdb as ddb

    con = ddb.get_connection()
    queries = QUERY_SETS[queryset]
    records = []

    print(f"{'query':18} {'tgt':5} {'policy':12} {'cell%':7} {'grid%':7} "
          f"{'miscert%':9} {'exact%':7} {'errP95':8} {'hwP50':8} {'speedup':8}")
    print("-" * 104)

    for qname, sql in queries.items():
        try:
            parsed = parse_analytical_query(sql)
            truth = _truth(con, sql, parsed)
        except Exception as exc:
            print(f"{qname:18} SKIPPED ({type(exc).__name__}: {str(exc)[:40]})")
            continue

        # The certified surface: the OLA arm is only defined where an interval
        # layer exists at all, which is the same admission test the engine uses.
        ci_join = parsed.has_joins and join_ci_is_defensible(parsed)
        if parsed.has_joins and not ci_join:
            print(f"{qname:18} SKIPPED (outside the certified join class)")
            continue
        evaluator = evaluate_join_sample_accuracy if ci_join else evaluate_sample_accuracy

        t0 = time.perf_counter()
        con.execute(sql).fetchall()
        exact_ms = (time.perf_counter() - t0) * 1000.0

        for target in TARGETS:
            plan = resolve_sampling_plan(parsed, "duckdb", "balanced", target)
            progression = list(plan["progression"])
            if target is not None:
                eps = max(0.005, min(0.5, 1.0 - float(target) / 100.0))
            else:
                eps = float(plan["convergence_threshold"])

            for policy in ("aetherquery", "ola"):
                out = []
                failed = False
                for _ in range(trials):
                    try:
                        if policy == "aetherquery":
                            trial = _run_aetherquery(run_approx, parsed, sql, target)
                        else:
                            trial = _run_ola(evaluator, parse_analytical_query, sql,
                                             "duckdb", progression, eps)
                    except Exception as exc:
                        print(f"{qname:18} {policy} FAILED "
                              f"({type(exc).__name__}: {str(exc)[:40]})")
                        failed = True
                        break
                    trial["_truth"] = truth
                    out.append(trial)
                if failed:
                    continue
                rec = _summarise(policy, qname, sql, target, trials, out, exact_ms)
                rec["epsilon"] = eps
                rec["progression"] = progression
                records.append(rec)
                print(f"{qname:18} {str(target):5} {policy:12} "
                      f"{rec['cell_coverage_pct']:7.1f} {rec['grid_coverage_pct']:7.1f} "
                      f"{rec['mis_certification_pct']:9.1f} {rec['exact_fallback_pct']:7.0f} "
                      f"{rec['rel_err_p95_pct']:8.3f} {rec['reported_half_width_p50_pct']:8.3f} "
                      f"{rec['speedup']:8.2f}")

    try:
        sha = subprocess.check_output(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        sha = "unknown"
    try:
        import duckdb as _dk
        duckdb_version = _dk.__version__
    except Exception:
        duckdb_version = "unknown"

    import datetime
    payload = {
        "provenance": {
            "generated_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "engine_git_sha": sha,
            "duckdb_version": duckdb_version,
            "python_version": platform.python_version(),
            "host": platform.platform(),
            "database": str(Path(database).resolve()),
            "queryset": queryset,
            "trials": trials,
            "coverage_level": COVERAGE_LEVEL,
            "experiment": "aetherquery_vs_online_aggregation_style_baseline",
            "shared_ladder": "backend.core.runtime_sampling.resolve_sampling_plan",
            "baseline_note": (
                "online-aggregation-style per-cell stopping discipline implemented "
                "on AetherQuery's own estimator layer; not a reproduction of any "
                "named published system"
            ),
        },
        "records": records,
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWritten: {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", default="aqp_eval/datasets/tpch_sf1.duckdb")
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--output", default="aqp_eval/results/baseline_comparison_sf1.json")
    ap.add_argument("--queryset", choices=["tpch", "tpcds"], default="tpch")
    args = ap.parse_args()
    run(args.database, args.trials, args.output, queryset=args.queryset)
