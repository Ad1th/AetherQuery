"""
Thesis-B experiment: does engine-native block sampling break interval validity?

Hypothesis
----------
``TABLESAMPLE SYSTEM`` selects whole physical blocks rather than individual
rows. When rows inside a block are correlated, the sample carries far less
information than its row count suggests, so intervals computed under a
simple-random-sampling assumption under-cover. Treating the block as the
sampling unit should restore coverage.

Design: two factors, fully crossed
----------------------------------
    layout      shuffled  -- rows in random order, blocks representative
                clustered -- rows sorted by value, blocks homogeneous

    predicate   correlated  -- WHERE x > threshold, i.e. filtering on the same
                               column the table is ordered by. The realistic
                               case: a table loaded in date order and queried
                               by date range.
                independent -- a predicate unrelated to physical position.

Crossing them separates two claims that are easy to conflate. Clustering only
matters for a given aggregate if *that aggregate's* inputs are what got
clustered, and the independent-predicate arm is what shows this rather than
assuming it.

Everything else is held fixed: same rows, same true answers, same sampler,
same estimators. Any coverage difference is attributable to layout alone.

On block size
-------------
DuckDB samples 2048-row vectors (measured). Simulating that directly needs
millions of rows before a 5% sample contains enough blocks to estimate a
between-block variance, which is impractical in pure Python. So the simulation
uses a smaller block and reports the *intraclass correlation* rho, which is a
property of the data layout and does not depend on block size. Kish's identity
``DEFF = 1 + (m-1)*rho`` then converts that rho to whatever block size an
engine actually uses -- and since DEFF grows with block size, DuckDB's 2048 is
strictly worse than whatever the simulation shows.

    python scripts/run_design_effect_study.py
    python scripts/run_design_effect_study.py --trials 2000 --block-size 512
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

from backend.stats.contracts import Aggregate, Method
from backend.stats.design_effect import (
    DUCKDB_VECTOR_SIZE,
    estimate_clustered,
    estimate_design_effect,
    intraclass_correlation,
    minimum_blocks_for_valid_interval,
    project_design_effect,
)
from backend.stats.estimators import estimate_aggregate
from backend.stats.simulation import (
    Population,
    block_sampler,
    cluster_stats_for,
    normal_population,
    reorder,
    sample_stats_for,
    srswor,
    wilson_interval,
)

AGGREGATES = (Aggregate.COUNT, Aggregate.SUM, Aggregate.AVG)


def banner(title: str) -> None:
    print(f"\n{'=' * 84}\n{title}\n{'=' * 84}")


def verdict_for(covered: int, resolved: int, nominal: float = 0.95) -> str:
    if resolved == 0:
        return "ABSTAIN"
    return "PASS" if wilson_interval(covered, resolved)[1] >= nominal else "UNDER"


def with_predicate(population: Population, kind: str, seed: int = 31) -> Population:
    """
    Rebuild the WHERE predicate without touching values or row order.

    ``correlated`` selects on the value itself, so on a value-ordered table the
    domain is physically contiguous. ``independent`` selects at random, so the
    domain is scattered however the table is ordered.
    """
    if kind == "correlated":
        threshold = statistics.median(population.values)
        flags = [value > threshold for value in population.values]
    elif kind == "independent":
        rng = random.Random(seed)
        flags = [rng.random() < 0.5 for _ in population.values]
    else:
        raise ValueError(f"unknown predicate kind: {kind}")

    return Population(
        name=f"{population.name}/{kind}",
        values=list(population.values),
        in_domain=flags,
        groups=population.groups,
    )


def run_experiment(
    population: Population,
    aggregate: Aggregate,
    estimator: str,
    trials: int,
    fraction: float,
    block_size: int,
    seed: int = 4242,
) -> dict:
    """One coverage experiment under block sampling."""
    truth = population.truth(aggregate)
    sampler = block_sampler(block_size)
    rng = random.Random(seed)
    size = len(population)

    covered = resolved = 0
    widths: list[float] = []
    deffs: list[float] = []
    blocks_seen: list[int] = []

    for _ in range(trials):
        indices = sampler(size, fraction, rng)

        if estimator == "srs":
            estimate = estimate_aggregate(
                aggregate, sample_stats_for(population, indices), method=Method.CLT
            )
        else:
            cluster = cluster_stats_for(population, indices, block_size=block_size)
            blocks_seen.append(cluster.blocks_sampled)
            estimate = estimate_clustered(aggregate, cluster)
            deff = estimate_design_effect(aggregate, cluster)
            if deff is not None:
                deffs.append(deff)

        if not estimate.is_usable:
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
        "deff": statistics.median(deffs) if deffs else None,
        "blocks": statistics.fmean(blocks_seen) if blocks_seen else None,
        "verdict": verdict_for(covered, resolved),
    }


def header() -> None:
    print(
        f"{'configuration':<44} {'cover':>8} {'95% CI':>16} {'relhw':>9} "
        f"{'DEFF':>9}  verdict"
    )
    print("-" * 100)


def print_row(label: str, result: dict) -> None:
    coverage = result["coverage"]
    coverage_text = "n/a" if coverage is None else f"{coverage:7.2%}"
    low, high = wilson_interval(result["covered"], result["resolved"])
    interval = "n/a" if coverage is None else f"[{low:.3f},{high:.3f}]"
    width = result["width"]
    width_text = "n/a" if width is None else f"{width:8.4f}"
    deff = result["deff"]
    deff_text = "-" if deff is None else f"{deff:8.1f}"
    print(
        f"{label:<44} {coverage_text:>8} {interval:>16} {width_text:>9} "
        f"{deff_text:>9}  {result['verdict']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=800)
    parser.add_argument("--size", type=int, default=200_000)
    parser.add_argument("--block-size", type=int, default=256)
    parser.add_argument("--fraction", type=float, default=0.05)
    args = parser.parse_args()

    base = normal_population(size=args.size)
    layouts = {
        "shuffled": reorder(base, "shuffled"),
        "clustered": reorder(base, "clustered"),
    }

    total_blocks = -(-args.size // args.block_size)
    blocks_per_draw = max(1, round(total_blocks * args.fraction))

    banner("SETUP")
    print(f"  population       : {args.size:,} rows")
    print(f"  block size       : {args.block_size} rows  ({total_blocks:,} blocks)")
    print(f"  sampling         : block sampler at {args.fraction:.0%}")
    print(f"  blocks per draw  : {blocks_per_draw}  ({blocks_per_draw - 1} df)")
    print(f"  rows per draw    : {blocks_per_draw * args.block_size:,}")
    print(f"  trials           : {args.trials}")
    if blocks_per_draw < 20:
        print(
            f"\n  WARNING: only {blocks_per_draw} blocks per draw. The between-block\n"
            "  variance is barely identified and the cluster estimator will be\n"
            "  very wide. Raise --size or lower --block-size."
        )

    banner("1. BASELINE  --  row sampling, SRS intervals. Must pass.")
    header()
    for aggregate in AGGREGATES:
        population = with_predicate(layouts["shuffled"], "independent")
        truth = population.truth(aggregate)
        rng = random.Random(4242)
        covered = resolved = 0
        for _ in range(args.trials):
            indices = srswor(len(population), args.fraction, rng)
            estimate = estimate_aggregate(
                aggregate, sample_stats_for(population, indices)
            )
            if not estimate.is_usable:
                continue
            resolved += 1
            if estimate.ci_low <= truth <= estimate.ci_high:
                covered += 1
        print_row(
            f"srswor / srs / {aggregate.value}",
            {
                "covered": covered,
                "resolved": resolved,
                "coverage": covered / resolved if resolved else None,
                "width": None,
                "deff": None,
                "verdict": verdict_for(covered, resolved),
            },
        )
    print("\n  Sanity check. If these fail, nothing below means anything.")

    results: dict = {}

    for predicate_kind in ("correlated", "independent"):
        banner(
            f"2{'ab'[predicate_kind == 'independent']}. BLOCK SAMPLING  --  "
            f"{predicate_kind} predicate"
        )
        if predicate_kind == "correlated":
            print("  WHERE x > median: the filter selects on the ordering column.\n")
        else:
            print("  A predicate unrelated to physical position.\n")
        header()

        for layout_name, layout in layouts.items():
            population = with_predicate(layout, predicate_kind)
            for estimator in ("srs", "cluster"):
                for aggregate in AGGREGATES:
                    key = (predicate_kind, layout_name, estimator, aggregate)
                    results[key] = run_experiment(
                        population,
                        aggregate,
                        estimator,
                        args.trials,
                        args.fraction,
                        args.block_size,
                    )
                    print_row(
                        f"{layout_name} / {estimator} / {aggregate.value}",
                        results[key],
                    )
            print()

    banner("3. INTRACLASS CORRELATION, AND WHAT IT IMPLIES FOR DUCKDB")
    print(
        f"{'predicate':<13} {'layout':<11} {'agg':<7} {'DEFF@' + str(args.block_size):>10} "
        f"{'rho':>10} {'DEFF@2048':>11} {'n_eff/n':>10}"
    )
    print("-" * 76)
    for predicate_kind in ("correlated", "independent"):
        for layout_name in ("shuffled", "clustered"):
            for aggregate in AGGREGATES:
                deff = results[
                    (predicate_kind, layout_name, "cluster", aggregate)
                ]["deff"]
                if deff is None:
                    continue
                rho = intraclass_correlation(deff, args.block_size)
                projected = project_design_effect(deff, args.block_size)
                if projected is None:
                    # A DEFF at or below 1.0 is estimation noise around "no
                    # clustering". Projecting it manufactures a confident
                    # absurdity -- an effective sample larger than the sample.
                    print(
                        f"{predicate_kind:<13} {layout_name:<11} "
                        f"{aggregate.value:<7} {deff:10.1f} "
                        f"{'~0':>10} {'none':>11} {'1.0':>10}"
                    )
                    continue
                rho_text = "n/a" if rho is None else f"{rho:.5f}"
                print(
                    f"{predicate_kind:<13} {layout_name:<11} {aggregate.value:<7} "
                    f"{deff:10.1f} {rho_text:>10} {projected:11.1f} "
                    f"{1.0 / projected:10.5f}"
                )
    print(
        "\n  rho is a property of the physical layout, independent of block size.\n"
        "  DEFF@2048 projects it onto DuckDB's actual sampling granularity;\n"
        "  n_eff/n is the fraction of a DuckDB sample that carries information.\n"
        "  'none' means the measured DEFF sat at or below 1.0, so no clustering\n"
        "  was detected and there is nothing to project."
    )
    print(
        "\n  Blocks needed before a cluster interval can be trusted: "
        f"{minimum_blocks_for_valid_interval(Aggregate.SUM)} for COUNT/SUM, "
        f"{minimum_blocks_for_valid_interval(Aggregate.AVG)} for AVG.\n"
        f"  This draw used {blocks_per_draw}."
    )

    banner("VERDICT")
    supported, unsupported = [], []
    for aggregate in AGGREGATES:
        clustered_srs = results[("correlated", "clustered", "srs", aggregate)]
        shuffled_srs = results[("correlated", "shuffled", "srs", aggregate)]
        repaired = results[("correlated", "clustered", "cluster", aggregate)]
        control = results[("independent", "clustered", "srs", aggregate)]

        c1 = clustered_srs["verdict"] == "UNDER"
        c2 = shuffled_srs["verdict"] == "PASS"
        c3 = repaired["verdict"] == "PASS"

        print(f"\n  {aggregate.value.upper()}")
        print(
            f"    clustered layout, SRS interval under-covers   "
            f"{'YES' if c1 else 'no ':>4}  ({clustered_srs['coverage']:.2%})"
        )
        print(
            f"    shuffled layout, SRS interval still covers    "
            f"{'YES' if c2 else 'no ':>4}  ({shuffled_srs['coverage']:.2%})"
        )
        print(
            f"    cluster estimator repairs the clustered case  "
            f"{'YES' if c3 else 'no ':>4}  ({repaired['coverage']:.2%})"
        )
        print(
            f"    [control] independent predicate, unaffected   "
            f"{'    ':>4}  ({control['coverage']:.2%})"
        )

        (supported if (c1 and c2 and c3) else unsupported).append(aggregate)

    print()
    if supported:
        print(
            f"  THESIS B SUPPORTED for: "
            f"{', '.join(a.value.upper() for a in supported)}"
        )
        print(
            "    Block sampling on physically clustered data invalidates\n"
            "    SRS-assumption intervals; treating the block as the sampling\n"
            "    unit repairs them."
        )
    if unsupported:
        print(
            f"\n  NOT SUPPORTED for: "
            f"{', '.join(a.value.upper() for a in unsupported)}"
        )
        print(
            "    A negative result to report as such, not to tune away.\n"
            "    See the per-aggregate lines above for the failing condition."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
