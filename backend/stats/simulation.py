"""
Coverage simulator for the estimator library.

A confidence interval that claims 95% coverage is making a falsifiable
prediction: sample a population whose true value you already know, many times,
and the interval should contain that truth in 95% of trials. This module runs
that experiment.

It is the only way to distinguish a correct estimator from a plausible-looking
one. Formula errors, missing corrections, and inapplicable asymptotics all look
fine in a single run and only reveal themselves as systematic under-coverage
across many.

Three things this module deliberately does
------------------------------------------
1. **Puts an error bar on the coverage estimate itself.** Empirical coverage is
   a proportion measured from a finite number of trials, so it has its own
   sampling error. Declaring 94.1% a failure against a nominal 95% is only
   meaningful once you know whether 94.1% is distinguishable from 95% at the
   trial count used. Every result carries a Wilson interval, and the PASS/FAIL
   verdict is based on that, not on the point estimate.

2. **Reports interval width alongside coverage.** A method can reach 100%
   coverage by returning intervals so wide they are useless. Coverage alone
   cannot rank methods; coverage plus width can.

3. **Counts unresolved trials rather than dropping them.** A trial where the
   estimator declined to produce an interval is not a trial that passed. The
   resolution rate is reported separately so that a method which quietly
   abstains most of the time cannot look accurate.

Sampling designs
----------------
``srswor`` is simple random sampling without replacement -- the design every
formula in estimators.py assumes. ``bernoulli`` includes each row
independently. Both are *row-level* designs. Engine-native
``TABLESAMPLE SYSTEM`` is a *block* design and violates the independence these
estimators assume; that is the subject of separate work, and the sampler
protocol here is deliberately shaped so a block sampler can be dropped in
without changing the harness.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from backend.stats.api import AggregateCell, estimate_query
from backend.stats.contracts import (
    Aggregate,
    Correction,
    Method,
    SampleStats,
)
from backend.stats.estimators import estimate_aggregate
from backend.stats.intervals import normal_quantile


# ----------------------------------------------------------------------
# populations
# ----------------------------------------------------------------------


@dataclass
class Population:
    """
    A synthetic table with known ground truth.

    Stored columnar so a sample is just a list of row indices.

    ``in_domain`` models a WHERE predicate; ``groups`` models a GROUP BY key
    (None for an ungrouped population). Ground truth is computed by brute force
    over every row, which is the point -- it is what the estimates are compared
    against.
    """

    name: str
    values: list[float]
    in_domain: list[bool]
    groups: list[Any] | None = None

    def __len__(self) -> int:
        return len(self.values)

    @property
    def group_keys(self) -> list[Any]:
        if self.groups is None:
            return [None]
        return sorted(set(self.groups), key=repr)

    def _rows(self, group: Any = None) -> list[float]:
        if self.groups is None or group is None:
            return [v for v, d in zip(self.values, self.in_domain) if d]
        return [
            v
            for v, d, g in zip(self.values, self.in_domain, self.groups)
            if d and g == group
        ]

    def truth(self, aggregate: Aggregate, group: Any = None) -> float | None:
        """The exact answer, computed over the whole population."""
        rows = self._rows(group)
        if aggregate is Aggregate.COUNT:
            return float(len(rows))
        if aggregate is Aggregate.SUM:
            return float(sum(rows))
        if aggregate is Aggregate.AVG:
            return (sum(rows) / len(rows)) if rows else None
        raise ValueError(f"unsupported aggregate: {aggregate}")

    def describe(self) -> str:
        rows = self._rows()
        if len(rows) < 2:
            return f"{self.name}: N={len(self)} domain={len(rows)}"
        mean = statistics.fmean(rows)
        sd = statistics.stdev(rows)
        skew = (
            sum(((v - mean) / sd) ** 3 for v in rows) / len(rows) if sd > 0 else 0.0
        )
        return (
            f"{self.name}: N={len(self):,} domain={len(rows):,} "
            f"mean={mean:,.3f} sd={sd:,.3f} skew={skew:+.2f}"
        )


def _domain_flags(size: int, domain_rate: float, rng: random.Random) -> list[bool]:
    return [rng.random() < domain_rate for _ in range(size)]


def normal_population(
    size: int = 40_000,
    mean: float = 50.0,
    stdev: float = 12.0,
    domain_rate: float = 0.35,
    seed: int = 1,
) -> Population:
    """Symmetric and light-tailed: the case the CLT is built for."""
    rng = random.Random(seed)
    return Population(
        name="normal",
        values=[rng.gauss(mean, stdev) for _ in range(size)],
        in_domain=_domain_flags(size, domain_rate, rng),
    )


def lognormal_population(
    size: int = 40_000,
    mu: float = 3.0,
    sigma: float = 0.9,
    domain_rate: float = 0.35,
    seed: int = 2,
) -> Population:
    """
    Right-skewed, like revenue or price columns.

    Skewness is ``(e^(s^2) + 2) * sqrt(e^(s^2) - 1)``, so sigma controls how
    hard the CLT is stressed: sigma=0.9 gives skew of about 4.1.
    """
    rng = random.Random(seed)
    return Population(
        name=f"lognormal(s={sigma})",
        values=[math.exp(rng.gauss(mu, sigma)) for _ in range(size)],
        in_domain=_domain_flags(size, domain_rate, rng),
    )


def pareto_population(
    size: int = 40_000,
    alpha: float = 1.8,
    domain_rate: float = 0.35,
    seed: int = 3,
) -> Population:
    """
    Heavy-tailed. With alpha < 3 the skewness is not even finite, which is the
    worst realistic case for a normal approximation.
    """
    rng = random.Random(seed)
    return Population(
        name=f"pareto(a={alpha})",
        values=[(1.0 - rng.random()) ** (-1.0 / alpha) for _ in range(size)],
        in_domain=_domain_flags(size, domain_rate, rng),
    )


def zero_inflated_population(
    size: int = 40_000,
    zero_rate: float = 0.7,
    mean: float = 40.0,
    stdev: float = 10.0,
    domain_rate: float = 0.35,
    seed: int = 4,
) -> Population:
    """Mostly zeros with occasional real values -- sparse metric columns."""
    rng = random.Random(seed)
    values = [
        0.0 if rng.random() < zero_rate else abs(rng.gauss(mean, stdev))
        for _ in range(size)
    ]
    return Population(
        name=f"zero_inflated({zero_rate:.0%})",
        values=values,
        in_domain=_domain_flags(size, domain_rate, rng),
    )


def constant_population(
    size: int = 40_000,
    value: float = 7.0,
    domain_rate: float = 0.35,
    seed: int = 5,
) -> Population:
    """
    Zero variance. Every sample produces an identically zero sample variance,
    so this is the degenerate case: the estimators must not report a zero-width
    interval as certainty.
    """
    rng = random.Random(seed)
    return Population(
        name="constant",
        values=[value] * size,
        in_domain=_domain_flags(size, domain_rate, rng),
    )


def offset_population(
    size: int = 40_000,
    offset: float = 1e6,
    stdev: float = 1.0,
    domain_rate: float = 0.35,
    seed: int = 6,
) -> Population:
    """
    Large mean, tiny spread -- the numerical hazard of the pushdown design.

    Variance is recovered from ``SUM(x*x)`` computed inside the database, and
    ``sum_xx - sum_x^2/n`` subtracts two nearly equal enormous numbers. At an
    offset of 1e6 with unit spread the true variance is about 1e-12 of the
    magnitude of either term, so double precision retains only a few digits of
    it. If catastrophic cancellation is going to break these estimators, it
    breaks here.
    """
    rng = random.Random(seed)
    return Population(
        name=f"offset({offset:.0e})",
        values=[offset + rng.gauss(0.0, stdev) for _ in range(size)],
        in_domain=_domain_flags(size, domain_rate, rng),
    )


def grouped_population(
    size: int = 40_000,
    num_groups: int = 12,
    zipf_exponent: float = 1.1,
    mean: float = 50.0,
    stdev: float = 15.0,
    domain_rate: float = 1.0,
    seed: int = 7,
) -> Population:
    """
    Zipf-distributed group sizes: a few large groups and a long tail of tiny
    ones. Realistic, and the case that stresses per-group intervals -- the
    small groups are small samples no matter how large the table is.
    """
    rng = random.Random(seed)
    weights = [1.0 / ((i + 1) ** zipf_exponent) for i in range(num_groups)]
    total = sum(weights)
    weights = [w / total for w in weights]

    groups: list[Any] = []
    for _ in range(size):
        draw = rng.random()
        cumulative = 0.0
        for index, weight in enumerate(weights):
            cumulative += weight
            if draw <= cumulative:
                groups.append(f"g{index:02d}")
                break
        else:
            groups.append(f"g{num_groups - 1:02d}")

    return Population(
        name=f"grouped(k={num_groups})",
        values=[abs(rng.gauss(mean, stdev)) for _ in range(size)],
        in_domain=_domain_flags(size, domain_rate, rng),
        groups=groups,
    )


ALL_POPULATIONS: dict[str, Callable[[], Population]] = {
    "normal": normal_population,
    "lognormal": lognormal_population,
    "pareto": pareto_population,
    "zero_inflated": zero_inflated_population,
    "constant": constant_population,
    "offset": offset_population,
}


# ----------------------------------------------------------------------
# sampling designs
# ----------------------------------------------------------------------

Sampler = Callable[[int, float, random.Random], Sequence[int]]


def srswor(size: int, fraction: float, rng: random.Random) -> Sequence[int]:
    """Simple random sampling without replacement -- the assumed design."""
    return rng.sample(range(size), max(1, int(round(size * fraction))))


def bernoulli(size: int, fraction: float, rng: random.Random) -> Sequence[int]:
    """Independent per-row inclusion. Sample size is itself random."""
    return [i for i in range(size) if rng.random() < fraction]


def block_sampler(block_size: int = 2048):
    """
    Build a sampler that mimics engine-native ``TABLESAMPLE SYSTEM``.

    Chooses whole blocks of contiguous row positions and returns every row in
    each. This reproduces the behaviour measured on DuckDB, where a sampled
    result is 99.96% contiguous in runs of exactly 2048 with run starts on
    multiples of 2048.

    Physical position matters here in a way it never does under row sampling:
    the sampler picks *positions*, so how the rows were laid out determines
    what ends up in a block together.
    """

    def sample(size: int, fraction: float, rng: random.Random) -> Sequence[int]:
        total_blocks = math.ceil(size / block_size)
        wanted = max(1, round(total_blocks * fraction))
        wanted = min(wanted, total_blocks)
        chosen = rng.sample(range(total_blocks), wanted)
        indices: list[int] = []
        for block in chosen:
            start = block * block_size
            indices.extend(range(start, min(start + block_size, size)))
        return indices

    return sample


def reorder(population: Population, mode: str, seed: int = 99) -> Population:
    """
    Rewrite a population's physical row order, leaving its contents identical.

    The comparison that isolates the design effect. Same rows, same true
    answers, same sampler -- only the layout differs, so any change in coverage
    is attributable to physical clustering and nothing else.

    ``shuffled``  random order; no relationship between position and value,
                  so blocks are representative and DEFF should sit near 1.
    ``clustered`` sorted by value; each block is internally near-homogeneous
                  and wildly unrepresentative of the whole.
    ``banded``    sorted by group key, the realistic case for a table loaded
                  in date or key order and then grouped on that column.
    """
    order = list(range(len(population)))

    if mode == "shuffled":
        random.Random(seed).shuffle(order)
    elif mode == "clustered":
        order.sort(key=lambda i: population.values[i])
    elif mode == "banded":
        if population.groups is None:
            raise ValueError("banded reordering needs a grouped population")
        order.sort(key=lambda i: (repr(population.groups[i]), population.values[i]))
    else:
        raise ValueError(f"unknown reorder mode: {mode}")

    return Population(
        name=f"{population.name}/{mode}",
        values=[population.values[i] for i in order],
        in_domain=[population.in_domain[i] for i in order],
        groups=(
            [population.groups[i] for i in order]
            if population.groups is not None
            else None
        ),
    )


def cluster_stats_for(
    population: Population,
    indices: Iterable[int],
    group: Any = None,
    *,
    block_size: int = 2048,
) -> "ClusterSampleStats":
    """
    Turn a block sample into per-block sufficient statistics.

    Mirrors adding ``rowid // block_size`` to the GROUP BY of the pushed-down
    query -- one extra key on a query the engine is already running.
    """
    from backend.stats.design_effect import BlockAggregate, ClusterSampleStats

    values = population.values
    flags = population.in_domain
    groups = population.groups

    accumulator: dict[int, list[float]] = {}

    for index in indices:
        block_id = index // block_size
        cell = accumulator.setdefault(block_id, [0, 0, 0.0, 0.0])
        cell[0] += 1
        if not flags[index]:
            continue
        if group is not None and groups is not None and groups[index] != group:
            continue
        value = values[index]
        cell[1] += 1
        cell[2] += value
        cell[3] += value * value

    blocks = tuple(
        BlockAggregate(
            block_id=block_id,
            n_rows=int(cell[0]),
            n_domain=int(cell[1]),
            sum_x=cell[2],
            sum_xx=cell[3],
        )
        for block_id, cell in sorted(accumulator.items())
    )

    return ClusterSampleStats(
        blocks=blocks,
        total_blocks=math.ceil(len(population) / block_size),
        population_size=len(population),
        block_size=block_size,
    )


# ----------------------------------------------------------------------
# turning a sample into sufficient statistics
# ----------------------------------------------------------------------


def sample_stats_for(
    population: Population,
    indices: Iterable[int],
    group: Any = None,
    *,
    design_effect: float = 1.0,
    stable_variance: bool = False,
) -> SampleStats:
    """
    Compute the sufficient statistics a database would push down.

    Mirrors what ``SELECT COUNT(*), SUM(x), SUM(x*x), MIN(x), MAX(x)`` returns
    over a sampled, filtered, grouped scan -- including accumulating ``sum_xx``
    in the same naive way the engine would, so that numerical issues in the
    pushdown design surface here rather than being hidden by a more careful
    local computation.

    ``stable_variance=True`` additionally supplies ``variance_direct``, computed
    by a numerically stable two-pass method, standing in for the engine's
    ``VAR_SAMP(x)``. Toggling it isolates exactly how much of an estimator's
    error is caused by the reconstruction rather than by the sampling.
    """
    indices = list(indices)
    n_domain = 0
    sum_x = 0.0
    sum_xx = 0.0
    sum_xxx = 0.0
    min_x: float | None = None
    max_x: float | None = None

    values = population.values
    flags = population.in_domain
    groups = population.groups

    observed: list[float] = []

    for index in indices:
        if not flags[index]:
            continue
        if group is not None and groups is not None and groups[index] != group:
            continue
        value = values[index]
        n_domain += 1
        sum_x += value
        sum_xx += value * value
        sum_xxx += value * value * value
        if min_x is None or value < min_x:
            min_x = value
        if max_x is None or value > max_x:
            max_x = value
        if stable_variance:
            observed.append(value)

    variance_direct: float | None = None
    if stable_variance and len(observed) >= 2:
        variance_direct = statistics.variance(observed)

    return SampleStats(
        n_sample=len(indices),
        n_domain=n_domain,
        sum_x=sum_x,
        sum_xx=sum_xx,
        min_x=min_x,
        max_x=max_x,
        sum_xxx=sum_xxx,
        population_size=len(population),
        design_effect=design_effect,
        variance_direct=variance_direct,
    )


def sample_stats_by_group(
    population: Population,
    indices: Iterable[int],
    *,
    design_effect: float = 1.0,
) -> dict[Any, SampleStats]:
    """
    Sufficient statistics for every group, in a single pass over the sample.

    Equivalent to calling ``sample_stats_for`` once per group but O(n) rather
    than O(groups * n), which matters because the grouped studies dominate the
    simulator's runtime. Groups with no sampled rows are still present in the
    result, carrying ``n_domain = 0``, so that an unobserved group is visible
    to the caller rather than silently absent.
    """
    indices = list(indices)
    n_sample = len(indices)

    values = population.values
    flags = population.in_domain
    groups = population.groups

    accumulator: dict[Any, list[Any]] = {
        key: [0, 0.0, 0.0, None, None] for key in population.group_keys
    }

    for index in indices:
        if not flags[index]:
            continue
        key = groups[index] if groups is not None else None
        cell = accumulator[key]
        value = values[index]
        cell[0] += 1
        cell[1] += value
        cell[2] += value * value
        if cell[3] is None or value < cell[3]:
            cell[3] = value
        if cell[4] is None or value > cell[4]:
            cell[4] = value

    return {
        key: SampleStats(
            n_sample=n_sample,
            n_domain=cell[0],
            sum_x=cell[1],
            sum_xx=cell[2],
            min_x=cell[3],
            max_x=cell[4],
            population_size=len(population),
            design_effect=design_effect,
        )
        for key, cell in accumulator.items()
    }


# ----------------------------------------------------------------------
# coverage measurement
# ----------------------------------------------------------------------


def wilson_interval(
    successes: int, trials: int, confidence: float = 0.95
) -> tuple[float, float]:
    """
    Wilson score interval for a proportion.

    Used on the *coverage estimate itself*. Preferred over the normal
    approximation here because measured coverage sits near 1.0, where the
    normal interval misbehaves and can even exceed 1.
    """
    if trials == 0:
        return (0.0, 1.0)

    z = normal_quantile(confidence)
    p = successes / trials
    denominator = 1.0 + z * z / trials
    center = (p + z * z / (2.0 * trials)) / denominator
    margin = (
        z
        * math.sqrt(p * (1.0 - p) / trials + z * z / (4.0 * trials * trials))
        / denominator
    )
    return (max(0.0, center - margin), min(1.0, center + margin))


@dataclass(frozen=True)
class CoverageResult:
    """Outcome of one coverage experiment."""

    population: str
    aggregate: Aggregate
    method: Method
    fraction: float
    trials: int
    resolved: int
    covered: int
    nominal: float
    truth: float | None
    mean_estimate: float | None
    mean_relative_half_width: float | None
    mean_n_domain: float

    @property
    def empirical(self) -> float | None:
        """Coverage among trials that produced an interval."""
        if self.resolved == 0:
            return None
        return self.covered / self.resolved

    @property
    def resolution_rate(self) -> float:
        return self.resolved / self.trials if self.trials else 0.0

    @property
    def coverage_interval(self) -> tuple[float, float]:
        return wilson_interval(self.covered, self.resolved)

    @property
    def relative_bias(self) -> float | None:
        """Mean estimate minus truth, as a fraction of truth."""
        if self.truth in (None, 0) or self.mean_estimate is None:
            return None
        return (self.mean_estimate - self.truth) / abs(self.truth)

    @property
    def verdict(self) -> str:
        """
        PASS, UNDER, DEGENERATE, or ABSTAIN.

        The test is one-sided by design. Over-covering is conservative and
        therefore safe -- a valid-but-loose bound is still valid. Only
        *under*-coverage falsifies the claim, so the verdict fails when the
        upper end of the Wilson interval still sits below nominal, i.e. when
        under-coverage is not explainable by the finite trial count.

        DEGENERATE is reported separately for intervals of exactly zero width.
        Those achieve perfect coverage on a constant population, but by
        coincidence rather than by validity: the sample variance was zero, so
        the interval collapsed to a point that happened to be the right one.
        Scoring that as PASS would let a broken estimator look perfect, so it
        gets its own label.
        """
        if self.resolution_rate < 0.5:
            return "ABSTAIN"
        if self.empirical is None:
            return "ABSTAIN"
        if (
            self.method is not Method.CENSUS
            and self.mean_relative_half_width == 0.0
        ):
            return "DEGENERATE"
        return "PASS" if self.coverage_interval[1] >= self.nominal else "UNDER"


def run_coverage_study(
    population: Population,
    aggregate: Aggregate,
    fraction: float = 0.05,
    trials: int = 2000,
    method: Method = Method.CLT,
    coverage_level: float = 0.95,
    sampler: Sampler = srswor,
    group: Any = None,
    seed: int = 12345,
    stable_variance: bool = False,
) -> CoverageResult:
    """
    Repeatedly sample a known population and measure how often the interval
    contains the truth.

    Trials where the estimator declined to produce an interval are counted as
    unresolved rather than discarded silently; see ``resolution_rate``.
    """
    truth = population.truth(aggregate, group)
    rng = random.Random(seed)
    size = len(population)

    covered = 0
    resolved = 0
    estimates: list[float] = []
    half_widths: list[float] = []
    domain_sizes: list[int] = []

    for _ in range(trials):
        indices = sampler(size, fraction, rng)
        stats = sample_stats_for(
            population, indices, group, stable_variance=stable_variance
        )
        domain_sizes.append(stats.n_domain)

        estimate = estimate_aggregate(aggregate, stats, coverage_level, method)
        if not estimate.is_usable:
            continue

        resolved += 1
        estimates.append(estimate.estimate)
        if estimate.relative_half_width is not None:
            half_widths.append(estimate.relative_half_width)
        if truth is not None and estimate.ci_low <= truth <= estimate.ci_high:
            covered += 1

    return CoverageResult(
        population=population.name,
        aggregate=aggregate,
        method=method,
        fraction=fraction,
        trials=trials,
        resolved=resolved,
        covered=covered,
        nominal=coverage_level,
        truth=truth,
        mean_estimate=statistics.fmean(estimates) if estimates else None,
        mean_relative_half_width=(
            statistics.fmean(half_widths) if half_widths else None
        ),
        mean_n_domain=statistics.fmean(domain_sizes) if domain_sizes else 0.0,
    )


@dataclass(frozen=True)
class SimultaneousCoverageResult:
    """
    Outcome of a family-wise coverage experiment over a grouped query.

    ``simultaneous`` is the fraction of trials in which *every* interval in the
    result grid contained its truth -- the thing a multiplicity correction
    claims to control. ``marginal`` is the per-interval rate, which is what an
    uncorrected method delivers. The gap between the two is the entire reason
    corrections exist.
    """

    population: str
    correction: Correction
    method: Method
    fraction: float
    trials: int
    groups: int
    aggregates: int
    nominal: float
    all_covered: int
    complete_trials: int
    marginal_covered: int
    marginal_total: int
    incomplete_trials: int
    mean_relative_half_width: float | None

    @property
    def simultaneous(self) -> float | None:
        if self.complete_trials == 0:
            return None
        return self.all_covered / self.complete_trials

    @property
    def marginal(self) -> float | None:
        if self.marginal_total == 0:
            return None
        return self.marginal_covered / self.marginal_total

    @property
    def coverage_interval(self) -> tuple[float, float]:
        return wilson_interval(self.all_covered, self.complete_trials)

    @property
    def verdict(self) -> str:
        if self.complete_trials < max(1, self.trials // 2):
            return "ABSTAIN"
        if self.simultaneous is None:
            return "ABSTAIN"
        return "PASS" if self.coverage_interval[1] >= self.nominal else "UNDER"


def run_simultaneous_coverage_study(
    population: Population,
    aggregates: Sequence[Aggregate] = (Aggregate.COUNT, Aggregate.SUM),
    fraction: float = 0.05,
    trials: int = 1000,
    method: Method = Method.CLT,
    coverage_level: float = 0.95,
    correction: Correction = Correction.BONFERRONI,
    sampler: Sampler = srswor,
    seed: int = 54321,
) -> SimultaneousCoverageResult:
    """
    Measure family-wise coverage over a grouped query.

    A trial passes only when every interval in the grid covers its truth. Trials
    in which some group was never sampled are counted as incomplete and
    excluded from the simultaneous rate -- an unobserved group is a distinct
    failure mode (see the unseen-group problem) and conflating it with an
    interval that missed would muddle both numbers.
    """
    group_keys = population.group_keys
    truths = {
        (group, aggregate): population.truth(aggregate, group)
        for group in group_keys
        for aggregate in aggregates
    }

    rng = random.Random(seed)
    size = len(population)

    all_covered = 0
    complete_trials = 0
    incomplete_trials = 0
    marginal_covered = 0
    marginal_total = 0
    half_widths: list[float] = []

    for _ in range(trials):
        indices = sampler(size, fraction, rng)

        stats_by_group = sample_stats_by_group(population, indices)

        cells = []
        for group in group_keys:
            stats = stats_by_group[group]
            for aggregate in aggregates:
                cells.append(
                    AggregateCell(
                        alias=aggregate.value,
                        aggregate=aggregate,
                        stats=stats,
                        group_key=(group,),
                    )
                )

        result = estimate_query(
            cells, coverage_level, method, correction
        )

        trial_complete = True
        trial_all_covered = True

        for group in group_keys:
            for aggregate in aggregates:
                estimate = result.estimates[(group,)][aggregate.value]
                truth = truths[(group, aggregate)]
                if truth is None:
                    continue
                if not estimate.is_usable:
                    trial_complete = False
                    continue
                marginal_total += 1
                if estimate.relative_half_width is not None:
                    half_widths.append(estimate.relative_half_width)
                if estimate.ci_low <= truth <= estimate.ci_high:
                    marginal_covered += 1
                else:
                    trial_all_covered = False

        if trial_complete:
            complete_trials += 1
            if trial_all_covered:
                all_covered += 1
        else:
            incomplete_trials += 1

    return SimultaneousCoverageResult(
        population=population.name,
        correction=correction,
        method=method,
        fraction=fraction,
        trials=trials,
        groups=len(group_keys),
        aggregates=len(aggregates),
        nominal=coverage_level,
        all_covered=all_covered,
        complete_trials=complete_trials,
        marginal_covered=marginal_covered,
        marginal_total=marginal_total,
        incomplete_trials=incomplete_trials,
        mean_relative_half_width=(
            statistics.fmean(half_widths) if half_widths else None
        ),
    )


# ----------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------


def format_coverage_table(results: Sequence[CoverageResult]) -> str:
    """Fixed-width table of coverage results, for logs and the paper."""
    header = (
        f"{'population':<22} {'agg':<6} {'method':<20} {'f':>6} "
        f"{'n_dom':>8} {'cover':>8} {'95% CI':>16} {'relhw':>9} "
        f"{'res':>6}  verdict"
    )
    lines = [header, "-" * len(header)]

    for result in results:
        empirical = result.empirical
        low, high = result.coverage_interval
        coverage_text = "n/a" if empirical is None else f"{empirical:7.2%}"
        interval_text = "n/a" if empirical is None else f"[{low:.3f},{high:.3f}]"
        width_text = (
            "n/a"
            if result.mean_relative_half_width is None
            else f"{result.mean_relative_half_width:8.4f}"
        )
        lines.append(
            f"{result.population:<22} {result.aggregate.value:<6} "
            f"{result.method.value:<20} {result.fraction:6.3f} "
            f"{result.mean_n_domain:8.0f} {coverage_text:>8} "
            f"{interval_text:>16} {width_text:>9} "
            f"{result.resolution_rate:5.0%}  {result.verdict}"
        )

    return "\n".join(lines)


def summarize(results: Sequence[CoverageResult]) -> dict[str, int]:
    """Count verdicts, for a one-line pass/fail summary."""
    counts: dict[str, int] = defaultdict(int)
    for result in results:
        counts[result.verdict] += 1
    return dict(counts)
