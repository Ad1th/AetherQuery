"""
Optional-stopping experiment: what does peeking cost, and does spending fix it?

The controller in ``backend/core/runtime_sampling.py`` samples, inspects,
samples more, and stops when the answer looks settled. Fixed-sample intervals
assume exactly one look. This measures the gap that creates, and whether an
alpha-spending schedule closes it.

Three stopping rules are compared, all on identical data
--------------------------------------------------------
    oracle       stop at a fixed, pre-chosen look. No data-dependent stopping,
                 so this is the control -- fixed-sample intervals are valid
                 here by construction and must come out at nominal.

    precision    stop at the first look whose interval is narrow enough. The
                 rule a statistically-designed controller would use.

    convergence  stop when two successive estimates agree to within a
                 threshold. This is what AetherQuery actually does today, and
                 it selects for stability rather than accuracy: two samples can
                 agree closely and both be wrong.

    foregone     stop as soon as the interval excludes a reference value. The
                 textbook way to break a fixed-sample interval -- keep looking
                 and you cross by chance eventually. Included as a positive
                 control: if the correction cannot rescue this, it does not
                 work, and if the naive intervals survive it, the harness is
                 not measuring what it claims to.

Each rule is run twice: once with naive fixed-level intervals recomputed at
every look, and once through ``SequentialAnalysis``. Coverage is measured *at
the stopping time*, which is the only place it matters -- that is the interval
the user is shown.

    python scripts/run_sequential_study.py
    python scripts/run_sequential_study.py --trials 4000
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from backend.stats.api import AggregateCell, estimate_query
from backend.stats.contracts import Aggregate, Correction, Method
from backend.stats.sequential import (
    Schedule,
    SequentialAnalysis,
    price_of_peeking,
)
from backend.stats.simulation import (
    lognormal_population,
    normal_population,
    sample_stats_for,
    srswor,
    wilson_interval,
)

PROGRESSION = [0.01, 0.02, 0.05, 0.10, 0.20]
TARGET_RELATIVE_ERROR = 0.05
CONVERGENCE_THRESHOLD = 0.04  # the value balanced mode uses today


def banner(title: str) -> None:
    print(f"\n{'=' * 84}\n{title}\n{'=' * 84}")


def verdict_for(covered: int, resolved: int, nominal: float = 0.95) -> str:
    if resolved == 0:
        return "ABSTAIN"
    return "PASS" if wilson_interval(covered, resolved)[1] >= nominal else "UNDER"


def run_trial(population, aggregate, rule, corrected, rng):
    """
    Walk one progression under one stopping rule.

    Returns the interval the user would actually be shown -- the one at the
    stopping time -- or None if no look produced a usable interval.
    """
    analysis = (
        SequentialAnalysis(
            coverage_level=0.95,
            max_looks=len(PROGRESSION),
            correction=Correction.NONE,
        )
        if corrected
        else None
    )

    previous_estimate = None
    last_usable = None

    for look, fraction in enumerate(PROGRESSION, start=1):
        indices = srswor(len(population), fraction, rng)
        stats = sample_stats_for(population, indices)
        cells = [
            AggregateCell(alias=aggregate.value, aggregate=aggregate, stats=stats)
        ]

        if corrected:
            result = analysis.look(cells)
            estimate = result.estimates.estimates[None][aggregate.value]
        else:
            # The naive approach: a fresh nominal-95% interval every look, with
            # no accounting for the fact that this is not the first look.
            estimate_set = estimate_query(
                cells,
                coverage_level=0.95,
                method=Method.CLT,
                correction=Correction.NONE,
            )
            estimate = estimate_set.estimates[None][aggregate.value]

        if not estimate.is_usable:
            previous_estimate = estimate.estimate
            continue

        last_usable = estimate

        if rule == "oracle":
            if look == 3:
                return estimate
            previous_estimate = estimate.estimate
            continue

        if rule == "precision":
            if (
                estimate.relative_half_width is not None
                and estimate.relative_half_width <= TARGET_RELATIVE_ERROR
            ):
                return estimate

        elif rule == "convergence":
            if previous_estimate not in (None, 0):
                delta = abs(estimate.estimate - previous_estimate) / abs(
                    previous_estimate
                )
                if look >= 2 and delta < CONVERGENCE_THRESHOLD:
                    return estimate

        elif rule == "foregone":
            # Positive control. Thresholds the coverage statistic itself, which
            # is the specific thing that breaks fixed-sample intervals.
            reference = population.truth(aggregate)
            if reference is not None and not (
                estimate.ci_low <= reference <= estimate.ci_high
            ):
                return estimate

        previous_estimate = estimate.estimate

    return last_usable


def measure(population, aggregate, rule, corrected, trials, seed=606):
    truth = population.truth(aggregate)
    rng = random.Random(seed)
    covered = resolved = 0
    widths = []
    for _ in range(trials):
        estimate = run_trial(population, aggregate, rule, corrected, rng)
        if estimate is None or not estimate.is_usable:
            continue
        resolved += 1
        if estimate.relative_half_width is not None:
            widths.append(estimate.relative_half_width)
        if truth is not None and estimate.ci_low <= truth <= estimate.ci_high:
            covered += 1
    return {
        "covered": covered,
        "resolved": resolved,
        "coverage": covered / resolved if resolved else None,
        "width": statistics.fmean(widths) if widths else None,
        "verdict": verdict_for(covered, resolved),
    }


def header() -> None:
    print(
        f"{'rule':<14} {'intervals':<12} {'cover':>8} {'95% CI':>16} "
        f"{'relhw':>9}  verdict"
    )
    print("-" * 74)


def print_row(rule, kind, result):
    coverage = result["coverage"]
    coverage_text = "n/a" if coverage is None else f"{coverage:7.2%}"
    low, high = wilson_interval(result["covered"], result["resolved"])
    interval = "n/a" if coverage is None else f"[{low:.3f},{high:.3f}]"
    width = result["width"]
    width_text = "n/a" if width is None else f"{width:8.4f}"
    print(
        f"{rule:<14} {kind:<12} {coverage_text:>8} {interval:>16} "
        f"{width_text:>9}  {result['verdict']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=2000)
    parser.add_argument("--size", type=int, default=40_000)
    args = parser.parse_args()

    banner("SETUP")
    print(f"  progression      : {PROGRESSION}")
    print(f"  precision target : {TARGET_RELATIVE_ERROR:.0%} relative half-width")
    print(f"  convergence rule : successive estimates within {CONVERGENCE_THRESHOLD:.0%}")
    print(f"  trials           : {args.trials}")

    banner("1. THE PRICE OF PEEKING")
    print(
        "  What alpha-spending costs, before asking what it buys.\n"
        f"  Uniform schedule over {len(PROGRESSION)} looks, 95% overall.\n"
    )
    print(f"  {'look':>5} {'level':>10} {'width vs naive 95%':>22}")
    print("  " + "-" * 39)
    for look, level, multiplier in price_of_peeking(
        0.95, len(PROGRESSION), Schedule.UNIFORM
    ):
        print(f"  {look:>5} {level:>9.4%} {multiplier:>21.3f}x")
    print(f"\n  {'look':>5} {'level':>10} {'width vs naive 95%':>22}   (harmonic)")
    print("  " + "-" * 39)
    for look, level, multiplier in price_of_peeking(
        0.95, len(PROGRESSION), Schedule.HARMONIC
    ):
        print(f"  {look:>5} {level:>9.4%} {multiplier:>21.3f}x")

    populations = [
        ("normal", normal_population(size=args.size)),
        ("lognormal", lognormal_population(size=args.size, sigma=0.7)),
    ]

    all_results = {}
    for population_name, population in populations:
        for aggregate in (Aggregate.SUM, Aggregate.AVG):
            banner(
                f"2. COVERAGE AT THE STOPPING TIME  --  "
                f"{population_name} / {aggregate.value}"
            )
            header()
            for rule in ("oracle", "precision", "convergence", "foregone"):
                for corrected in (False, True):
                    key = (population_name, aggregate, rule, corrected)
                    all_results[key] = measure(
                        population, aggregate, rule, corrected, args.trials
                    )
                    print_row(
                        rule,
                        "sequential" if corrected else "naive 95%",
                        all_results[key],
                    )
                print()

    banner("FINDINGS")

    print("\n  a) Is the control sound?")
    oracle_bad = [
        key
        for key, result in all_results.items()
        if key[2] == "oracle" and not key[3] and result["verdict"] == "UNDER"
    ]
    if oracle_bad:
        print(
            "     NO -- fixed-look intervals under-cover, so the comparison\n"
            "     below is measuring something other than optional stopping."
        )
        for key in oracle_bad:
            print(f"       {key[0]} / {key[1].value}: "
                  f"{all_results[key]['coverage']:.2%}")
    else:
        print(
            "     Yes. With a pre-chosen look, naive intervals cover at nominal,\n"
            "     so any shortfall below is attributable to the stopping rule."
        )

    print("\n  b) Does data-dependent stopping break naive intervals?")
    for rule in ("precision", "convergence", "foregone"):
        damaged = [
            (key, result)
            for key, result in all_results.items()
            if key[2] == rule and not key[3] and result["verdict"] == "UNDER"
        ]
        if damaged:
            worst = min(damaged, key=lambda item: item[1]["coverage"])
            print(
                f"     {rule:<12} yes -- under-covers in "
                f"{len(damaged)} of {len(populations) * 2} configurations, "
                f"worst {worst[1]['coverage']:.2%} "
                f"({worst[0][0]}/{worst[0][1].value})"
            )
        else:
            print(f"     {rule:<12} no measurable damage at this trial count")

    print("\n  c) Does alpha-spending repair it?")
    for rule in ("precision", "convergence", "foregone"):
        corrected_bad = [
            key
            for key, result in all_results.items()
            if key[2] == rule and key[3] and result["verdict"] == "UNDER"
        ]
        naive_widths = [
            result["width"]
            for key, result in all_results.items()
            if key[2] == rule and not key[3] and result["width"]
        ]
        seq_widths = [
            result["width"]
            for key, result in all_results.items()
            if key[2] == rule and key[3] and result["width"]
        ]
        cost = (
            statistics.fmean(seq_widths) / statistics.fmean(naive_widths)
            if naive_widths and seq_widths
            else float("nan")
        )
        status = (
            "all configurations covered"
            if not corrected_bad
            else f"{len(corrected_bad)} configuration(s) still under"
        )
        print(f"     {rule:<12} {status}, at {cost:.2f}x the interval width")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
