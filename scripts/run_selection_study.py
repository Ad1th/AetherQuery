"""
Does Cochran's rule predict which intervals actually under-cover?

``selection.py`` uses ``n >= k * g1^2`` to decide whether the normal
approximation is defensible for a given cell. That rule is a textbook heuristic,
and adopting a heuristic on authority is exactly what this whole track exists to
avoid. So it is checked against the coverage simulator.

The rule is a *classifier*. For each configuration it predicts "CLT is fine" or
"CLT will under-cover", and the simulator measures which actually happened. A
useful classifier has few false negatives above all -- a cell it waves through
that then under-covers is a silent wrong answer, which is the failure mode that
matters. False positives merely cost interval width.

    python scripts/run_selection_study.py
    python scripts/run_selection_study.py --trials 4000
"""

from __future__ import annotations

import argparse
import os
import random
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from backend.stats.contracts import Aggregate, Method
from backend.stats.selection import observations_needed, recommend_method
from backend.stats.simulation import (
    lognormal_population,
    normal_population,
    pareto_population,
    run_coverage_study,
    sample_stats_for,
    srswor,
    zero_inflated_population,
)


def banner(title: str) -> None:
    print(f"\n{'=' * 88}\n{title}\n{'=' * 88}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=2000)
    parser.add_argument("--size", type=int, default=30_000)
    args = parser.parse_args()

    populations = [
        ("normal", normal_population(size=args.size)),
        ("lognormal(0.5)", lognormal_population(size=args.size, sigma=0.5)),
        ("lognormal(0.9)", lognormal_population(size=args.size, sigma=0.9)),
        ("lognormal(1.3)", lognormal_population(size=args.size, sigma=1.3)),
        ("pareto(2.5)", pareto_population(size=args.size, alpha=2.5)),
        ("pareto(1.8)", pareto_population(size=args.size, alpha=1.8)),
        ("zero_inflated", zero_inflated_population(size=args.size)),
    ]
    fractions = (0.01, 0.05, 0.20)

    banner("CLASSIFIER CHECK  --  Cochran's rule vs measured coverage")
    print(
        f"{'population':<16} {'agg':<5} {'f':>6} {'n':>7} {'skew':>8} "
        f"{'needs':>9} {'predicts':<10} {'coverage':>9} {'actual':<8} outcome"
    )
    print("-" * 104)

    outcomes = {"TP": 0, "TN": 0, "FP": 0, "FN": 0}
    false_negatives = []

    for population_name, population in populations:
        for aggregate in (Aggregate.SUM, Aggregate.AVG):
            for fraction in fractions:
                # Predict from several draws rather than one. Sample skewness
                # is itself noisy on heavy tails -- a single draw can miss the
                # tail entirely and report a deceptively tame value -- so a
                # one-sample prediction measures the draw as much as the rule.
                rng = random.Random(3)
                votes = []
                for _ in range(15):
                    stats = sample_stats_for(
                        population, srswor(len(population), fraction, rng)
                    )
                    votes.append(recommend_method(stats, aggregate))
                recommendation = max(
                    votes, key=lambda vote: vote.required_n or 0.0
                )
                predicts_ok = (
                    sum(1 for vote in votes if vote.method is Method.CLT)
                    > len(votes) / 2
                )

                result = run_coverage_study(
                    population,
                    aggregate,
                    fraction=fraction,
                    trials=args.trials,
                    method=Method.CLT,
                )
                if result.empirical is None:
                    continue
                actually_ok = result.verdict != "UNDER"

                if predicts_ok and actually_ok:
                    key, outcome = "TN", "ok"
                elif not predicts_ok and not actually_ok:
                    key, outcome = "TP", "caught"
                elif not predicts_ok and actually_ok:
                    key, outcome = "FP", "over-cautious"
                else:
                    key, outcome = "FN", "MISSED"
                    false_negatives.append(
                        (population_name, aggregate, fraction, result.empirical)
                    )
                outcomes[key] += 1

                skew = recommendation.skewness
                needs = recommendation.required_n
                print(
                    f"{population_name:<16} {aggregate.value:<5} {fraction:6.2f} "
                    f"{result.mean_n_domain:7.0f} "
                    f"{'n/a' if skew is None else f'{skew:+8.2f}'} "
                    f"{'n/a' if needs is None else f'{needs:9.0f}'} "
                    f"{'CLT ok' if predicts_ok else 'BERNSTEIN':<10} "
                    f"{result.empirical:8.2%} {result.verdict:<8} {outcome}"
                )

    banner("CLASSIFIER PERFORMANCE")
    total = sum(outcomes.values())
    caught = outcomes["TP"] + outcomes["TN"]
    print(f"  configurations tested        {total}")
    print(f"  correctly classified         {caught}  ({caught / total:.0%})")
    print(f"  true positives  (caught)     {outcomes['TP']}")
    print(f"  true negatives  (ok)         {outcomes['TN']}")
    print(f"  false positives (cautious)   {outcomes['FP']}   -- costs width only")
    print(f"  false negatives (MISSED)     {outcomes['FN']}   -- silent wrong answers")

    if outcomes["TP"] + outcomes["FN"]:
        recall = outcomes["TP"] / (outcomes["TP"] + outcomes["FN"])
        print(f"\n  recall on genuinely broken configurations: {recall:.0%}")

    if false_negatives:
        print("\n  Missed configurations -- the rule waved these through and they")
        print("  under-covered anyway:")
        for name, aggregate, fraction, coverage in false_negatives:
            print(
                f"    {name:<16} {aggregate.value:<5} f={fraction:.2f} "
                f"coverage={coverage:.2%}"
            )
        print(
            "\n  A non-empty list here means the rule is not conservative enough\n"
            "  and COCHRAN_CONSTANT should be raised further."
        )
    else:
        print(
            "\n  No false negatives: every configuration that actually under-covered\n"
            "  was predicted to. The rule is safe to key method selection on."
        )

    banner("WHAT THE RULE ASKS FOR")
    print(f"  {'skewness':>10} {'observations needed':>22}")
    print("  " + "-" * 34)
    for skew in (0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0):
        print(f"  {skew:>10.1f} {observations_needed(skew):>22.0f}")
    print(
        "\n  This is why a small GROUP BY group on a skewed column cannot be\n"
        "  rescued by sampling harder: the group is small because it is small."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
