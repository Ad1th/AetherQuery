"""
Design effect: what engine-native block sampling does to interval validity.

Every formula in estimators.py assumes simple random sampling -- each row
included independently of every other. Engine-native ``TABLESAMPLE SYSTEM``
does not work that way. It selects whole physical units and returns every row
in them, which is *cluster sampling*, and cluster sampling inflates variance by
the design effect::

    DEFF = 1 + (m_bar - 1) * rho          n_effective = n / DEFF

where ``m_bar`` is rows per sampled block and ``rho`` the within-block
correlation. The multiplier is brutal because ``m_bar`` is large: measured on
DuckDB, blocks are 2048 rows, so a within-block correlation of only 0.01
already implies DEFF near 21 and throws away 95% of the apparent sample.

Measured behaviour of DuckDB 1.x
--------------------------------
Probed directly rather than assumed (2,000,000-row table):

    TABLESAMPLE SYSTEM (1 PERCENT)     99.96% of sampled rows contiguous,
                                       runs of exactly 2048, run starts all
                                       multiples of 2048
    TABLESAMPLE BERNOULLI (1 PERCENT)  0.95% contiguous, runs of 1

So SYSTEM samples DuckDB *vectors* of 2048 rows, not row-group-sized chunks,
and BERNOULLI is genuine row-level sampling. Two consequences:

* The estimators are valid as written under BERNOULLI and invalid under
  SYSTEM. Switching the sampling clause is a legitimate alternative to
  correcting the variance, at whatever scan cost BERNOULLI carries.
* SYSTEM's realized fraction drifts far from the requested one -- a requested
  5% returned 3.38% and a requested 10% returned 8.70% in the same probe. Any
  estimator that scales by the *nominal* fraction is biased by that ratio.

Two ways to repair the variance, both provided here
---------------------------------------------------
1. ``estimate_clustered`` -- the textbook cluster estimator. Treats the block
   as the sampling unit and takes the variance *between block totals*. Exact
   for this design, needs no correlation estimate, and is the recommended path.
   Requires per-block sufficient statistics, which cost one extra GROUP BY key.

2. ``estimate_design_effect`` -- measures DEFF from the same block totals and
   returns a multiplier for ``SampleStats.design_effect``. Approximate, but it
   drops into the existing SRS pipeline without changing what is pushed down.

Getting a block identifier out of the engine
--------------------------------------------
DuckDB::

    SELECT rowid // 2048 AS block_id, ...   -- vector granularity, verified

PostgreSQL, where SYSTEM samples 8KB heap pages::

    SELECT (ctid::text::point)[0]::bigint AS block_id, ...

MySQL exposes no comparable physical locator, which is why the MySQL path
falls back to ``RAND()`` row sampling and does not need this module at all.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Sequence

from backend.stats.contracts import (
    Aggregate,
    Estimate,
    Method,
    SampleStats,
    undefined_estimate,
)
from backend.stats.estimators import DEFAULT_COVERAGE_LEVEL, _relative_half_width
from backend.stats.intervals import clt_half_width

DUCKDB_VECTOR_SIZE = 2048
"""DuckDB's sampling granularity for TABLESAMPLE SYSTEM, measured empirically."""

POSTGRES_PAGE_BYTES = 8192
"""PostgreSQL samples whole heap pages; rows per page depends on row width."""


@dataclass(frozen=True)
class BlockAggregate:
    """
    Sufficient statistics for one sampled block.

    Obtained by adding the block identifier to the GROUP BY of the sampled
    query -- one extra key, no extra pass::

        SELECT rowid // 2048 AS block_id,
               COUNT(*) AS n_rows,
               COUNT(x) FILTER (WHERE <predicate>) AS n_domain,
               SUM(x)   FILTER (WHERE <predicate>) AS sum_x
        FROM t TABLESAMPLE SYSTEM (5 PERCENT)
        GROUP BY block_id
    """

    block_id: Any
    n_rows: int
    n_domain: int
    sum_x: float = 0.0
    sum_xx: float = 0.0

    def __post_init__(self) -> None:
        if self.n_rows < 0 or self.n_domain < 0:
            raise ValueError("block row counts must be non-negative")
        if self.n_domain > self.n_rows:
            raise ValueError("n_domain cannot exceed n_rows within a block")


@dataclass(frozen=True)
class ClusterSampleStats:
    """
    A block sample: which blocks were drawn, and what each contained.

    ``total_blocks`` is M, the number of blocks in the whole relation, needed
    for the finite population correction at block level. For DuckDB it is
    ``ceil(N / 2048)``.
    """

    blocks: tuple[BlockAggregate, ...]
    total_blocks: int
    population_size: int
    block_size: int = DUCKDB_VECTOR_SIZE

    def __post_init__(self) -> None:
        if self.total_blocks < 1:
            raise ValueError("total_blocks must be at least 1")
        if len(self.blocks) > self.total_blocks:
            raise ValueError("sampled more blocks than exist")
        if self.population_size < 1:
            raise ValueError("population_size must be positive")

    # -- sizes ------------------------------------------------------------

    @property
    def blocks_sampled(self) -> int:
        """m."""
        return len(self.blocks)

    @property
    def block_fraction(self) -> float:
        """m / M -- the fraction that actually governs the sampling error."""
        return self.blocks_sampled / self.total_blocks

    @property
    def n_sample(self) -> int:
        return sum(block.n_rows for block in self.blocks)

    @property
    def n_domain(self) -> int:
        return sum(block.n_domain for block in self.blocks)

    @property
    def mean_block_size(self) -> float:
        """m_bar, the multiplier in Kish's design effect."""
        if not self.blocks:
            return 0.0
        return self.n_sample / self.blocks_sampled

    @property
    def sum_x(self) -> float:
        return sum(block.sum_x for block in self.blocks)

    @property
    def sum_xx(self) -> float:
        return sum(block.sum_xx for block in self.blocks)

    # -- block totals -----------------------------------------------------

    def block_totals(self, aggregate: Aggregate) -> list[float]:
        """
        The per-block quantity whose between-block variance is the real error.

        COUNT and SUM aggregate directly. AVG is a ratio, so its block totals
        are residuals ``sum_x_b - R * n_domain_b`` under the delta method; they
        sum to zero by construction, which is the giveaway that the ratio has
        been linearized correctly.
        """
        if aggregate is Aggregate.COUNT:
            return [float(block.n_domain) for block in self.blocks]
        if aggregate is Aggregate.SUM:
            return [block.sum_x for block in self.blocks]
        if aggregate is Aggregate.AVG:
            if self.n_domain == 0:
                return []
            ratio = self.sum_x / self.n_domain
            return [
                block.sum_x - ratio * block.n_domain for block in self.blocks
            ]
        raise ValueError(f"unsupported aggregate: {aggregate}")

    def as_srs(self, *, design_effect: float = 1.0) -> SampleStats:
        """
        Flatten to row-level statistics, discarding block structure.

        This is what the pipeline currently sees, and comparing an estimate
        built from it against ``estimate_clustered`` is exactly the measurement
        the design-effect experiment makes.
        """
        return SampleStats(
            n_sample=self.n_sample,
            n_domain=self.n_domain,
            sum_x=self.sum_x,
            sum_xx=self.sum_xx,
            min_x=None,
            max_x=None,
            population_size=self.population_size,
            design_effect=design_effect,
        )


# ----------------------------------------------------------------------
# the cluster estimator
# ----------------------------------------------------------------------


def estimate_clustered(
    aggregate: Aggregate,
    cluster: ClusterSampleStats,
    coverage_level: float = DEFAULT_COVERAGE_LEVEL,
) -> Estimate:
    """
    Estimate an aggregate treating the block, not the row, as the unit.

    For a total over blocks::

        Y_hat = (M / m) * sum of sampled block totals
        Var   = M^2 * (1 - m/M) * s_t^2 / m

    where ``s_t^2`` is the sample variance *of the block totals*. Structurally
    identical to the row-level formula, with blocks substituted for rows, which
    is the whole point: once the block is the unit, the independence assumption
    is satisfied again and no correlation ever has to be estimated.

    The binding constraint is that ``s_t^2`` needs at least two sampled blocks.
    Sampling 1% of a small table can return a single block, in which case there
    is no valid interval at any confidence level and this returns an undefined
    estimate rather than a fabricated one.
    """
    srs_view = cluster.as_srs()

    if cluster.blocks_sampled < 2:
        return undefined_estimate(
            aggregate,
            srs_view,
            coverage_level,
            f"only {cluster.blocks_sampled} block(s) sampled; a between-block "
            "variance needs at least 2, so no interval is available at this "
            "sampling rate regardless of how many rows were returned",
            point=_cluster_point_estimate(aggregate, cluster),
        )

    if aggregate is Aggregate.AVG and cluster.n_domain == 0:
        return undefined_estimate(
            aggregate,
            srs_view,
            coverage_level,
            "domain never observed; a mean over an empty domain is undefined",
        )

    point = _cluster_point_estimate(aggregate, cluster)
    if point is None:
        return undefined_estimate(
            aggregate, srs_view, coverage_level, "point estimate unavailable"
        )

    totals = cluster.block_totals(aggregate)
    blocks_sampled = cluster.blocks_sampled
    total_blocks = cluster.total_blocks

    between_variance = statistics.variance(totals)
    block_fpc = max(0.0, 1.0 - cluster.block_fraction)

    variance = (
        (total_blocks ** 2) * block_fpc * between_variance / blocks_sampled
    )

    if aggregate is Aggregate.AVG:
        # Delta method: the residual totals estimate the variance of the
        # numerator's departure from R times the denominator, so divide by the
        # estimated denominator squared to get back to the ratio's scale.
        denominator = cluster.n_domain / cluster.block_fraction
        if denominator == 0:
            return undefined_estimate(
                aggregate, srs_view, coverage_level, "estimated domain size is zero"
            )
        variance = variance / (denominator ** 2)

    # Degrees of freedom for a cluster design come from the number of blocks,
    # not the number of rows. Sampling 30 blocks of 2048 rows gives 29 df, not
    # 61439 -- which is precisely why these intervals are so much wider.
    half_width = clt_half_width(variance, coverage_level, blocks_sampled - 1)

    notes = (
        f"cluster estimator: {blocks_sampled} blocks of ~{cluster.mean_block_size:.0f} "
        f"rows treated as the sampling unit, so the interval rests on "
        f"{blocks_sampled - 1} degrees of freedom rather than "
        f"{cluster.n_sample - 1}",
    )

    if blocks_sampled < 30:
        notes = notes + (
            f"only {blocks_sampled} blocks sampled; the between-block variance is "
            "itself poorly determined and this interval should be treated as "
            "indicative",
        )

    ci_low = point - half_width if half_width is not None else None
    ci_high = point + half_width if half_width is not None else None
    if ci_low is not None and aggregate is Aggregate.COUNT:
        ci_low = max(0.0, ci_low)

    return Estimate(
        aggregate=aggregate,
        estimate=point,
        variance=variance,
        standard_error=math.sqrt(variance) if variance >= 0 else None,
        ci_low=ci_low,
        ci_high=ci_high,
        half_width=half_width,
        relative_half_width=_relative_half_width(point, half_width),
        coverage_level=coverage_level,
        method=Method.CLUSTER_CLT,
        n_sample=cluster.n_sample,
        n_domain=cluster.n_domain,
        fraction=cluster.block_fraction,
        design_effect=1.0,
        notes=notes,
    )


def _cluster_point_estimate(
    aggregate: Aggregate, cluster: ClusterSampleStats
) -> float | None:
    """Point estimates are unaffected by the design; only the variance changes."""
    fraction = cluster.block_fraction
    if fraction <= 0:
        return None
    if aggregate is Aggregate.COUNT:
        return cluster.n_domain / fraction
    if aggregate is Aggregate.SUM:
        return cluster.sum_x / fraction
    if aggregate is Aggregate.AVG:
        if cluster.n_domain == 0:
            return None
        return cluster.sum_x / cluster.n_domain
    raise ValueError(f"unsupported aggregate: {aggregate}")


# ----------------------------------------------------------------------
# measuring the design effect
# ----------------------------------------------------------------------


def estimate_design_effect(
    aggregate: Aggregate,
    cluster: ClusterSampleStats,
) -> float | None:
    """
    DEFF measured from the sample: how much wider the truth is than SRS claims.

    Defined operationally as the ratio of the cluster variance to the variance
    the SRS formula would have reported on the same rows. A value of 1 means
    the block structure carries no information and the rows may as well have
    been drawn independently; a value of 40 means the sample is worth a
    fortieth of its apparent size.

    Returns None when it cannot be computed -- fewer than two blocks, or a
    degenerate SRS variance -- and the caller must not substitute 1.0, since
    "unknown" and "no design effect" are very different claims.
    """
    if cluster.blocks_sampled < 2:
        return None

    from backend.stats.estimators import estimate_aggregate

    clustered = estimate_clustered(aggregate, cluster)
    srs = estimate_aggregate(aggregate, cluster.as_srs())

    if clustered.variance is None or srs.variance is None:
        return None
    if srs.variance <= 0:
        return None

    return clustered.variance / srs.variance


def intraclass_correlation(
    design_effect: float, mean_block_size: float
) -> float | None:
    """
    Recover Kish's rho from a measured design effect.

    Inverts ``DEFF = 1 + (m_bar - 1) * rho``. Reported because rho, unlike
    DEFF, is a property of the data's physical layout alone and does not move
    with the block size -- which makes it the right number to compare across
    engines whose block sizes differ, and the right number to quote in a paper.

    Can come out slightly negative when blocks are more internally varied than
    the population as a whole; that is a real (if unusual) configuration, not
    an error, so it is returned rather than clamped.
    """
    if mean_block_size <= 1:
        return None
    return (design_effect - 1.0) / (mean_block_size - 1.0)


def project_design_effect(
    measured_deff: float,
    measured_block_size: float,
    target_block_size: float = DUCKDB_VECTOR_SIZE,
) -> float | None:
    """
    Carry a DEFF measured at one block size across to another.

    Goes through rho, which is the layout property and does not depend on block
    size, and back out through Kish's identity. This is what makes a simulation
    at a tractable block size say something about a real engine.

    Returns None when the measurement is at or below 1.0. A design effect below
    1 would mean cluster sampling beat simple random sampling, which does not
    happen for positive within-block correlation; such values are estimation
    noise around 1.0. Projecting them produces confident nonsense -- a DEFF of
    0.9 extrapolates to an "effective sample" several times larger than the
    sample itself -- so the honest answer is that no clustering was detected.
    """
    if measured_block_size <= 1 or target_block_size <= 1:
        return None
    if measured_deff <= 1.0:
        return None

    rho = intraclass_correlation(measured_deff, measured_block_size)
    if rho is None or rho <= 0:
        return None

    return 1.0 + (target_block_size - 1.0) * rho


def minimum_blocks_for_valid_interval(aggregate: Aggregate) -> int:
    """
    How many blocks must be sampled before a cluster interval can be trusted.

    Measured by simulation on maximally clustered data: coverage of the AVG
    cluster estimator against a nominal 95% ran 84.0% at 10 sampled blocks,
    91.2% at 39, 92.7% at 156, 94.7% at 312 and 94.8% at 625. The estimator is
    asymptotically correct -- these are the ratio estimator's residuals being
    non-normal at small block counts, not a wrong formula -- but it needs a few
    hundred blocks before the interval means what it says.

    COUNT and SUM are totals rather than ratios and settle sooner.

    Practical consequence at DuckDB's 2048-row blocks: a 5% sample reaches 300
    blocks only once the table passes roughly 12 million rows. Below that,
    block sampling cannot support a trustworthy interval at 5% however many
    rows come back, and the honest options are to sample a larger fraction, to
    switch to ``TABLESAMPLE BERNOULLI``, or to report no interval.
    """
    return 300 if aggregate is Aggregate.AVG else 100


def blocks_from_indices(
    indices: Sequence[int],
    block_size: int = DUCKDB_VECTOR_SIZE,
) -> dict[int, list[int]]:
    """Group row positions by the physical block they belong to."""
    grouped: dict[int, list[int]] = {}
    for index in indices:
        grouped.setdefault(index // block_size, []).append(index)
    return grouped
