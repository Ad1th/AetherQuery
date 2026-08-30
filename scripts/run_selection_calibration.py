"""
Calibrate the constant in Cochran's skewness rule against measured coverage.

`backend/stats/selection.py` decides per cell whether the normal approximation
is defensible using `n >= kappa * g1^2`. The textbook kappa is 25. This script
treats the rule as a binary classifier and measures, over a grid of synthetic
populations, how each candidate kappa performs against the coverage the normal
approximation actually delivers.

The measurement is done once and re-used for every kappa: whether a
configuration under-covers depends only on the population, the aggregate and
the sampling fraction, not on kappa; kappa only changes the rule's *prediction*.
So one coverage pass over the grid supports the whole sweep.

Two error types, deliberately not treated symmetrically:

  false negative   the rule predicts the normal approximation is fine and it
                   under-covers. A silently wrong answer.
  false positive   the rule routes a sound cell to the wider finite-sample
                   construction. Costs interval width only.

Usage:
    python scripts/run_selection_calibration.py --trials 2000 \
        --output aqp_eval/results/selection_calibration.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.stats.contracts import Aggregate, Method  # noqa: E402
from backend.stats.simulation import (  # noqa: E402
    lognormal_population,
    normal_population,
    pareto_population,
    run_coverage_study,
    sample_stats_for,
    srswor,
    zero_inflated_population,
)

KAPPAS = [25.0, 50.0, 100.0, 400.0]
FRACTIONS = (0.01, 0.05, 0.20)
SKEW_VOTES = 15


def _populations(size):
    return [
        ("normal", normal_population(size=size)),
        ("lognormal(0.5)", lognormal_population(size=size, sigma=0.5)),
        ("lognormal(0.9)", lognormal_population(size=size, sigma=0.9)),
        ("lognormal(1.3)", lognormal_population(size=size, sigma=1.3)),
        ("pareto(2.5)", pareto_population(size=size, alpha=2.5)),
        ("pareto(1.8)", pareto_population(size=size, alpha=1.8)),
        ("zero_inflated", zero_inflated_population(size=size)),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=2000)
    ap.add_argument("--size", type=int, default=30_000)
    ap.add_argument("--output",
                    default="aqp_eval/results/selection_calibration.json")
    args = ap.parse_args()

    rows = []
    print(f"{'population':<16} {'agg':<5} {'f':>6} {'n':>7} {'skew':>8} "
          f"{'n/g1^2':>9} {'coverage':>9} {'verdict':<8}")
    print("-" * 82)

    for pop_name, population in _populations(args.size):
        for aggregate in (Aggregate.SUM, Aggregate.AVG):
            for fraction in FRACTIONS:
                # Sample skewness is itself noisy on a heavy tail, so take the
                # most pessimistic of several draws, as the engine's own
                # recommender does on repeated looks.
                rng = random.Random(3)
                skews, ns = [], []
                for _ in range(SKEW_VOTES):
                    stats = sample_stats_for(
                        population, srswor(len(population), fraction, rng))
                    # The engine's rule uses the expanded variable's skew and
                    # n_sample for SUM, the domain's skew and n_domain for AVG
                    # (backend/stats/selection.recommend_method); mirror that.
                    if aggregate is Aggregate.SUM:
                        sk, n_eff = stats.expanded_skewness, stats.n_sample
                    else:
                        sk, n_eff = stats.domain_skewness, stats.n_domain
                    if sk is None or not n_eff:
                        continue
                    skews.append(abs(float(sk)))
                    ns.append(n_eff)
                if not skews:
                    continue
                # Worst (most skewed) of the votes, matching the engine's
                # pessimistic choice across repeated looks.
                skew = max(skews)
                n_obs = sum(ns) / len(ns)

                result = run_coverage_study(
                    population, aggregate, fraction=fraction,
                    trials=args.trials, method=Method.CLT)
                if result.empirical is None:
                    continue

                # The rule fires (routes to the finite-sample bound) whenever
                # n < kappa * g1^2, i.e. whenever kappa > n / g1^2.
                ratio = (n_obs / (skew * skew)) if skew > 0 else float("inf")
                rows.append({
                    "population": pop_name,
                    "aggregate": aggregate.value,
                    "fraction": fraction,
                    "mean_n_domain": result.mean_n_domain,
                    "sample_skewness_worst_of_15": skew,
                    "ratio_n_over_g1sq": ratio,
                    "clt_empirical_coverage": result.empirical,
                    "verdict": result.verdict,
                    "under_covers": result.verdict == "UNDER",
                })
                print(f"{pop_name:<16} {aggregate.value:<5} {fraction:6.2f} "
                      f"{result.mean_n_domain:7.0f} {skew:8.2f} "
                      f"{ratio:9.1f} {result.empirical:9.2%} "
                      f"{result.verdict:<8}")

    sweep = []
    n_under = sum(1 for r in rows if r["under_covers"])
    n_sound = len(rows) - n_under
    print(f"\n{len(rows)} configurations, {n_under} of which the normal "
          f"approximation under-covers.\n")
    print(f"{'kappa':>7} {'recall':>8} {'unflagged under-coverers':>26} "
          f"{'sound cells flagged':>21}")
    for kappa in KAPPAS:
        # flagged == the rule sends this cell to the finite-sample bound
        tp = sum(1 for r in rows
                 if r["under_covers"] and kappa > r["ratio_n_over_g1sq"])
        fn = n_under - tp
        fp = sum(1 for r in rows
                 if not r["under_covers"] and kappa > r["ratio_n_over_g1sq"])
        recall = (tp / n_under) if n_under else float("nan")
        sweep.append({
            "kappa": kappa, "true_positives": tp, "false_negatives": fn,
            "false_positives": fp, "recall": recall,
            "sound_configurations": n_sound,
            "under_covering_configurations": n_under,
        })
        print(f"{kappa:7.0f} {recall:8.0%} {fn:26d} "
              f"{f'{fp} of {n_sound}':>21}")

    try:
        sha = subprocess.check_output(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            text=True).strip()
    except Exception:
        sha = "unknown"

    payload = {
        "provenance": {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "engine_git_sha": sha,
            "python_version": platform.python_version(),
            "host": platform.platform(),
            "experiment": "cochran_constant_calibration",
            "trials_per_configuration": args.trials,
            "population_size": args.size,
            "fractions": list(FRACTIONS),
            "skew_votes": SKEW_VOTES,
            "description": (
                "Cochran's rule n >= kappa*g1^2 treated as a classifier for "
                "whether the normal-approximation interval under-covers; "
                "coverage measured once per configuration under SRS without "
                "replacement and re-used across the kappa sweep."
            ),
        },
        "configurations": rows,
        "kappa_sweep": sweep,
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
