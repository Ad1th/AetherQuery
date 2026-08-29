"""
Statistical contracts for AetherQuery approximate query processing.

This module defines the two structures that separate the *statistical* layer
from the *execution* layer:

    SampleStats -- sufficient statistics from one sampled query. Everything the
                   estimators need and nothing more. Every field is obtainable
                   from pushed-down SQL aggregates (COUNT / SUM / SUM of
                   squares / MIN / MAX), so sampled rows never have to be
                   materialized.

    Estimate    -- the result contract the sampling controller consumes: point
                   estimate, variance/SE, CI bounds, half-width, coverage
                   level, and the method that produced them.

Design notes
------------
* stdlib only. The estimator library must be provable and testable in
  isolation, so it has no dependency on pandas, numpy, or any DB driver.
* ``design_effect`` is carried with a default of 1.0. Under simple random
  sampling it stays 1.0; the block-sampling work (Thesis B) fills it in and
  every variance below scales by it automatically.
* "Domain" is survey-sampling terminology for the subset of rows a given
  estimate is about: the rows passing WHERE and belonging to one GROUP BY
  group. Domain membership is itself random under sampling, which is why
  COUNT and SUM variances are taken over the whole sample (see estimators.py).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Aggregate(str, Enum):
    """Aggregate functions the estimator library supports."""

    COUNT = "count"
    SUM = "sum"
    AVG = "avg"


class Method(str, Enum):
    """How a confidence interval was produced."""

    CLT = "clt"
    """Normal approximation. Tight, asymptotic; can under-cover on small n or
    heavy skew."""

    EMPIRICAL_BERNSTEIN = "empirical_bernstein"
    """Maurer-Pontil finite-sample bound. Valid at every n, uses the observed
    variance, needs bounded support."""

    HOEFFDING = "hoeffding"
    """Range-only finite-sample bound. Valid at every n, ignores the variance,
    usually very loose."""

    CLUSTER_CLT = "cluster_clt"
    """Normal approximation with the physical block, not the row, as the
    sampling unit. The correct treatment for engine-native block sampling."""

    CENSUS = "census"
    """Sampling fraction reached 1.0: the answer is exact, not estimated."""

    RULE_OF_THREE = "rule_of_three"
    """Zero domain observations. One-sided upper bound only."""

    DEGENERATE = "degenerate"
    """Every sampled value was identical, so the observed variance is exactly
    zero. That is not certainty -- it is an uninformative sample -- and no
    interval is reported."""

    UNDEFINED = "undefined"
    """No interval could be formed (too few observations, unknown range, or an
    undefined quantity such as AVG over an empty domain)."""


class Correction(str, Enum):
    """Multiplicity correction for reporting many intervals at once."""

    NONE = "none"
    BONFERRONI = "bonferroni"
    SIDAK = "sidak"


@dataclass(frozen=True)
class SampleStats:
    """
    Sufficient statistics for one (group, column) cell of a sampled query.

    All of these come from a single pushed-down aggregate query::

        SELECT grp,
               COUNT(*)  AS n_domain,
               SUM(x)    AS sum_x,
               SUM(x*x)  AS sum_xx,
               MIN(x)    AS min_x,
               MAX(x)    AS max_x
        FROM t TABLESAMPLE SYSTEM (5 PERCENT)
        WHERE <predicate>
        GROUP BY grp

    with ``n_sample`` coming from the same sample *before* the predicate and
    grouping are applied (one extra ungrouped COUNT(*) over the sample).

    Attributes
    ----------
    n_sample:
        Rows in the sample as a whole, before WHERE and before grouping. This
        is the sampling denominator; COUNT and SUM variances are computed over
        these ``n_sample`` observations, treating out-of-domain rows as zeros.
    n_domain:
        Sampled rows falling in this domain (passed WHERE, in this group).
    sum_x, sum_xx:
        Sum and sum-of-squares of the aggregated column over the domain rows.
        Both are zero for COUNT-only cells.
    min_x, max_x:
        Observed extremes of the column over the domain rows. Required by the
        Hoeffding and empirical-Bernstein methods, which need bounded support.
        None means "range unknown" and those methods decline to produce an
        interval rather than inventing one.
    population_size:
        N, the total row count of the sampled relation. Cheap to obtain
        (DuckDB answers COUNT(*) from metadata) and strongly preferred: it
        yields the *realized* sampling fraction rather than the nominal one.
    nominal_fraction:
        The fraction requested from TABLESAMPLE, used only when
        ``population_size`` is unknown. Engine-native block samplers do not
        return exactly this fraction, so estimates built on it carry a warning
        note.
    design_effect:
        Variance inflation from the sampling design (Kish's DEFF). 1.0 under
        simple random sampling. Block/SYSTEM sampling on physically clustered
        data can push this into the hundreds; see the Thesis-B work.
    domain_is_universe:
        True when the caller knows this domain is the entire relation -- an
        unfiltered, ungrouped aggregate. Only then can COUNT(*) be reported as
        exactly N with zero error. Left False, an all-rows-in-domain sample is
        treated as a statistical coincidence and bounded accordingly, which is
        the safe default but needlessly pessimistic for the very common
        ``SELECT COUNT(*) FROM t``.
    variance_direct:
        The within-domain sample variance computed by the engine itself, via
        ``VAR_SAMP(x)``. Strongly preferred over reconstructing it from
        ``sum_xx``. Reconstruction computes ``sum_xx - sum_x^2/n``, which
        subtracts two nearly equal large numbers whenever the column's mean
        dwarfs its spread; the measured result is that a column centered near
        1e8 with unit spread loses the variance entirely and interval coverage
        collapses from 95% to roughly 47%, silently. Engines compute VAR_SAMP
        by a numerically stable method, so supplying it here removes the whole
        failure mode. When present it takes precedence and ``sum_xx`` is used
        only for the range-based bounds.
    """

    n_sample: int
    n_domain: int
    sum_x: float = 0.0
    sum_xx: float = 0.0
    min_x: float | None = None
    max_x: float | None = None
    population_size: int | None = None
    nominal_fraction: float | None = None
    design_effect: float = 1.0
    domain_is_universe: bool = False
    variance_direct: float | None = None
    sum_xxx: float | None = None
    skewness_direct: float | None = None

    def __post_init__(self) -> None:
        if self.n_sample < 0:
            raise ValueError("n_sample must be non-negative")
        if self.n_domain < 0:
            raise ValueError("n_domain must be non-negative")
        if self.n_domain > self.n_sample:
            raise ValueError(
                f"n_domain ({self.n_domain}) cannot exceed n_sample ({self.n_sample})"
            )
        if self.population_size is not None:
            if self.population_size <= 0:
                raise ValueError("population_size must be positive")
            if self.n_sample > self.population_size:
                raise ValueError(
                    f"n_sample ({self.n_sample}) cannot exceed "
                    f"population_size ({self.population_size})"
                )
        if self.nominal_fraction is not None and not 0.0 < self.nominal_fraction <= 1.0:
            raise ValueError("nominal_fraction must lie in (0, 1]")
        if self.population_size is None and self.nominal_fraction is None:
            raise ValueError(
                "provide population_size (preferred) or nominal_fraction so the "
                "sampling fraction is known"
            )
        if self.design_effect <= 0:
            raise ValueError("design_effect must be positive")
        if self.min_x is not None and self.max_x is not None and self.min_x > self.max_x:
            raise ValueError("min_x cannot exceed max_x")

    # ------------------------------------------------------------------
    # sampling fraction
    # ------------------------------------------------------------------

    @property
    def fraction_is_realized(self) -> bool:
        """True when the fraction is measured (n/N) rather than requested."""
        return self.population_size is not None

    @property
    def fraction(self) -> float:
        """
        Sampling fraction f.

        Realized (``n_sample / N``) whenever the population size is known.
        This matters: engine-native SYSTEM sampling returns approximately, not
        exactly, the requested fraction, and scaling by the nominal value
        introduces a bias the realized value does not have.
        """
        if self.population_size is not None:
            return self.n_sample / self.population_size
        assert self.nominal_fraction is not None  # guaranteed by __post_init__
        return self.nominal_fraction

    @property
    def population_estimate(self) -> float:
        """N, known exactly or implied by the nominal fraction."""
        if self.population_size is not None:
            return float(self.population_size)
        return self.n_sample / self.fraction

    @property
    def is_census(self) -> bool:
        """The sample covers the whole relation, so there is no sampling error."""
        return self.fraction >= 1.0

    @property
    def finite_population_correction(self) -> float:
        """
        The ``(1 - f)`` factor.

        AetherQuery samples up to 50-100%, where this term dominates: at
        f = 0.5 it halves the variance, and at f = 1.0 it is exactly zero,
        correctly reporting that a full scan has no sampling error. Omitting
        it makes intervals badly over-wide at high sampling rates.
        """
        return max(0.0, 1.0 - self.fraction)

    # ------------------------------------------------------------------
    # moments
    # ------------------------------------------------------------------

    @property
    def domain_mean(self) -> float | None:
        """Sample mean of x over domain rows; None if the domain is empty."""
        if self.n_domain == 0:
            return None
        return self.sum_x / self.n_domain

    @property
    def domain_sum_of_squares(self) -> float | None:
        """
        Centered sum of squares within the domain: ``sum_xx - sum_x^2 / n_d``.

        Uses ``variance_direct`` when the engine supplied it, since that avoids
        the cancellation this subtraction is prone to. Clamped at zero:
        floating-point cancellation in a pushed-down ``SUM(x*x)`` can otherwise
        produce a small negative value when the column is near-constant.
        """
        if self.n_domain == 0:
            return None
        if self.variance_direct is not None:
            return max(0.0, (self.n_domain - 1) * self.variance_direct)
        return max(0.0, self.sum_xx - (self.sum_x ** 2) / self.n_domain)

    @property
    def domain_variance(self) -> float | None:
        """Unbiased sample variance of x within the domain; needs n_domain >= 2."""
        if self.n_domain < 2:
            return None
        if self.variance_direct is not None:
            return max(0.0, self.variance_direct)
        sum_squares = self.domain_sum_of_squares
        assert sum_squares is not None
        return sum_squares / (self.n_domain - 1)

    @property
    def expanded_variance(self) -> float | None:
        """
        Unbiased sample variance of ``y = x * 1[row in domain]`` over all
        ``n_sample`` sampled rows.

        Out-of-domain rows contribute y = 0 and are genuine observations of the
        expanded variable, so they belong in this variance. This is what makes
        a SUM variance computable even from a single domain row.

        When the engine supplied ``variance_direct`` this is rebuilt from it
        algebraically rather than from ``sum_xx``. The rearranged form adds two
        positive quantities instead of subtracting two large nearly equal ones,
        so it inherits the engine's numerical stability.
        """
        if self.n_sample < 2:
            return None

        if self.variance_direct is not None and self.n_domain >= 1:
            within = (self.n_domain - 1) * self.variance_direct
            between = (self.sum_x ** 2) * (
                (self.n_sample - self.n_domain) / (self.n_domain * self.n_sample)
            )
            return max(0.0, within + between) / (self.n_sample - 1)

        centered = self.sum_xx - (self.sum_x ** 2) / self.n_sample
        return max(0.0, centered) / (self.n_sample - 1)

    @property
    def variance_cancellation_ratio(self) -> float | None:
        """
        How much of ``sum_xx`` survives the centering subtraction.

        A ratio of 1e-12 means twelve of the roughly sixteen significant digits
        a float carries are destroyed by ``sum_xx - sum_x^2/n``, leaving four.
        None when the diagnostic does not apply (engine-supplied variance, or
        no data).
        """
        if self.variance_direct is not None:
            return None
        if self.n_domain < 2 or self.sum_xx <= 0:
            return None
        centered = self.sum_xx - (self.sum_x ** 2) / self.n_domain
        return abs(centered) / self.sum_xx

    @property
    def domain_skewness(self) -> float | None:
        """
        Sample skewness of x within the domain, bias-corrected.

        Skew is the single best predictor of whether the normal approximation
        will hold: the coverage study found COUNT robust on every population
        while SUM and AVG degraded monotonically with skew (95.4% normal ->
        93.7% lognormal -> 92.7% Pareto for SUM). Measuring it is what turns
        "prefer a finite-sample method sometimes" into a decidable rule.

        Prefers ``skewness_direct`` from the engine (DuckDB has ``skewness(x)``)
        because reconstructing a third moment from ``SUM(x*x*x)`` suffers the
        same cancellation that wrecks the variance, only worse.
        """
        if self.skewness_direct is not None:
            return self.skewness_direct
        if self.sum_xxx is None or self.n_domain < 3:
            return None

        n = self.n_domain
        mean = self.sum_x / n
        second_moment = self.sum_xx / n - mean * mean
        if second_moment <= 0:
            return None

        third_moment = (
            self.sum_xxx / n - 3.0 * mean * self.sum_xx / n + 2.0 * mean ** 3
        )
        skew = third_moment / (second_moment ** 1.5)
        # Fisher's bias correction for a sample of size n.
        return math.sqrt(n * (n - 1)) / (n - 2) * skew

    @property
    def expanded_skewness(self) -> float | None:
        """
        Sample skewness of ``y = x * 1[in domain]`` over all sampled rows.

        The right quantity for COUNT and SUM, whose estimators are totals of
        the expanded variable rather than means over the domain. It is usually
        *more* skewed than the domain alone, because the out-of-domain zeros
        are a spike at one end -- a moderately skewed column filtered down to a
        third of the table can be badly skewed once the zeros are counted, and
        judging such a case on ``domain_skewness`` would understate the
        problem.

        Computable from the same three sums, taken over ``n_sample``.
        """
        if self.sum_xxx is None or self.n_sample < 3:
            return None

        n = self.n_sample
        mean = self.sum_x / n
        second_moment = self.sum_xx / n - mean * mean
        if second_moment <= 0:
            return None

        third_moment = (
            self.sum_xxx / n - 3.0 * mean * self.sum_xx / n + 2.0 * mean ** 3
        )
        skew = third_moment / (second_moment ** 1.5)
        return math.sqrt(n * (n - 1)) / (n - 2) * skew

    @property
    def satisfies_cochran_rule(self) -> bool | None:
        """
        Whether the domain is large enough for the normal approximation, given
        its skew.

        Cochran's rule of thumb asks for ``n >= 25 * g1^2``, where g1 is the
        sample skewness, before a normal-approximation interval on a mean can be
        trusted. It encodes what the coverage study observed directly: size
        alone does not rescue a skewed column, and a symmetric column needs
        very little.

        None when skewness is unavailable, which the caller must not read as
        "satisfied".
        """
        skew = self.domain_skewness
        if skew is None:
            return None
        return self.n_domain >= 25.0 * skew * skew

    @property
    def variance_is_degenerate(self) -> bool:
        """
        Every sampled value in the domain was identical.

        The sample variance is then exactly zero, and a normal-approximation
        interval collapses to a point. That point interval is not certainty --
        it is the signature of an uninformative sample -- and reporting it as
        a zero-width interval would let a controller reading
        ``relative_half_width == 0.0`` declare instant convergence.
        """
        if self.n_domain < 2:
            return False
        variance = self.domain_variance
        return variance is not None and variance == 0.0

    @property
    def variance_precision_is_degraded(self) -> bool:
        """
        True when reconstructing the variance from ``sum_xx`` has lost so much
        precision that the resulting interval should not be trusted.

        Measured empirically: at a ratio of 1e-10 the recovered variance is
        still good to about six digits, at 1e-12 to about three, and by 1e-16
        it is wrong by orders of magnitude and coverage collapses to roughly
        half of nominal. The threshold is set where the error starts to matter,
        not where it becomes catastrophic.
        """
        ratio = self.variance_cancellation_ratio
        return ratio is not None and ratio < 1e-10

    @property
    def domain_rate(self) -> float:
        """p-hat: the fraction of sampled rows landing in this domain."""
        if self.n_sample == 0:
            return 0.0
        return self.n_domain / self.n_sample

    # ------------------------------------------------------------------
    # support
    # ------------------------------------------------------------------

    @property
    def domain_range(self) -> float | None:
        """Observed width of x within the domain, for range-based bounds."""
        if self.min_x is None or self.max_x is None:
            return None
        return float(self.max_x) - float(self.min_x)

    @property
    def expanded_range(self) -> float | None:
        """
        Width of the support of ``y = x * 1[in domain]``.

        Zero is always attainable (any out-of-domain row), so the support is
        ``[min(0, min_x), max(0, max_x)]`` and is at least as wide as the
        domain's own range.
        """
        if self.min_x is None or self.max_x is None:
            return None
        return max(0.0, float(self.max_x)) - min(0.0, float(self.min_x))


@dataclass(frozen=True)
class Estimate:
    """
    The statistical layer's output contract.

    This is what the sampling controller consumes to decide whether to stop.
    An interval is reported as ``[ci_low, ci_high]`` at ``coverage_level``,
    produced by ``method``. When no valid interval could be formed the bounds
    are None, ``method`` is UNDEFINED or RULE_OF_THREE, and ``notes`` says why.
    Callers must handle that case rather than assuming a number is always
    available.
    """

    aggregate: Aggregate
    estimate: float | None
    variance: float | None
    standard_error: float | None
    ci_low: float | None
    ci_high: float | None
    half_width: float | None
    relative_half_width: float | None
    coverage_level: float
    method: Method
    n_sample: int
    n_domain: int
    fraction: float
    design_effect: float = 1.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_usable(self) -> bool:
        """True when both a point estimate and a two-sided interval exist."""
        return (
            self.estimate is not None
            and self.ci_low is not None
            and self.ci_high is not None
        )

    def meets_target(self, target_relative_error: float) -> bool | None:
        """
        Stopping-rule predicate for the controller.

        Returns True/False when a relative half-width exists, and None when it
        does not (empty domain, single observation, zero-valued estimate). The
        controller must treat None as "keep sampling", never as success.
        """
        if self.relative_half_width is None:
            return None
        return self.relative_half_width <= target_relative_error

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form for the API layer."""
        return {
            "aggregate": self.aggregate.value,
            "estimate": self.estimate,
            "variance": self.variance,
            "standard_error": self.standard_error,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "half_width": self.half_width,
            "relative_half_width": self.relative_half_width,
            "coverage_level": self.coverage_level,
            "method": self.method.value,
            "n_sample": self.n_sample,
            "n_domain": self.n_domain,
            "fraction": self.fraction,
            "design_effect": self.design_effect,
            "notes": list(self.notes),
        }


def undefined_estimate(
    aggregate: Aggregate,
    stats: SampleStats,
    coverage_level: float,
    reason: str,
    *,
    point: float | None = None,
    method: Method = Method.UNDEFINED,
) -> Estimate:
    """Build an Estimate carrying a point value (possibly) but no interval."""
    return Estimate(
        aggregate=aggregate,
        estimate=point,
        variance=None,
        standard_error=None,
        ci_low=None,
        ci_high=None,
        half_width=None,
        relative_half_width=None,
        coverage_level=coverage_level,
        method=method,
        n_sample=stats.n_sample,
        n_domain=stats.n_domain,
        fraction=stats.fraction,
        design_effect=stats.design_effect,
        notes=(reason,),
    )
