"""
Point estimators and variance estimators for sampled aggregates.

Notation, fixed throughout
--------------------------
    N     population size of the sampled relation
    n_s   rows in the sample, before WHERE and before grouping
    n_d   sampled rows in this domain (passed WHERE, in this group)
    f     sampling fraction, realized as n_s / N wherever N is known
    x_i   the aggregated column on domain row i
    z_i   1 if population row i is in the domain, else 0
    y_i   x_i * z_i, i.e. the column value expanded by zeros outside the domain

Why COUNT and SUM take their variance over the whole sample
-----------------------------------------------------------
COUNT and SUM over a filtered/grouped query are *domain totals*: sums of y_i
and z_i over all N population rows. Domain membership is random under
sampling, and that randomness is part of the estimator's variance. The
expanded variables y and z are observed for every one of the n_s sampled rows
(out-of-domain rows are genuine zeros), so their variance is taken over n_s,
not n_d. Taking it over n_d instead -- the intuitive but wrong move -- drops
the variance contributed by which rows landed in the domain at all, and
under-states the error.

AVG is a ratio, not a mean
--------------------------
With GROUP BY the group's row count is itself random, so
``AVG = sum(y) / sum(z)`` is a ratio of two random quantities and its variance
is not ``s^2 / n``. It needs delta-method linearization on the residuals
``r_i = y_i - R * z_i``. The result reduces to ``(1-f) * s_x^2 / n_d`` in the
large-sample limit, which is reassuringly close to the naive form -- but the
exact expression is used here, and it differs materially for small groups.
"""

from __future__ import annotations

import math

from backend.stats.contracts import (
    Aggregate,
    Estimate,
    Method,
    SampleStats,
    undefined_estimate,
)
from backend.stats.intervals import (
    clt_half_width,
    empirical_bernstein_half_width,
    hoeffding_half_width,
    rule_of_three_upper_bound,
)

DEFAULT_COVERAGE_LEVEL = 0.95

_NOMINAL_FRACTION_NOTE = (
    "sampling fraction is nominal, not realized; engine-native SYSTEM sampling "
    "does not return exactly the requested fraction, so this estimate carries "
    "an unquantified scaling bias"
)
_DEFF_HEURISTIC_NOTE = (
    "design effect applied by substituting an effective sample size n/DEFF; "
    "this is a heuristic adjustment, not implied by the finite-sample theorem"
)
_NO_FPC_NOTE = (
    "finite-sample bounds do not take the finite population correction, so "
    "this interval is conservative at high sampling fractions"
)
_PRECISION_NOTE = (
    "variance reconstructed from SUM(x*x) has lost most of its significant "
    "digits to cancellation, because this column's mean dwarfs its spread; the "
    "interval below is unreliable. Supply variance_direct from the engine's "
    "VAR_SAMP(x) to remove this failure mode"
)


def _precision_notes(stats: SampleStats) -> tuple[str, ...]:
    """Warn when the pushed-down sum of squares can no longer carry a variance."""
    if stats.variance_precision_is_degraded:
        return (_PRECISION_NOTE,)
    return ()


def _degenerate_reason(stats: SampleStats, coverage_level: float) -> str:
    """
    Explain a refusal to report an interval on a constant sample, and say what
    *can* still be claimed.

    Observing n identical values does not establish that the column is
    constant. If a fraction p of the population differed, the chance of drawing
    none of them is ``(1-p)^n``, so the rule of three bounds how much of the
    population could be hiding a different value. That bounds the *rate* of
    differing rows, not how far they lie from what was seen, so it is reported
    as context rather than converted into an interval -- doing that would need
    population bounds on the column, which the sufficient statistics do not
    carry.
    """
    # A variance of exactly zero has two very different causes and the cancelled
    # sum cannot tell them apart -- genuine homogeneity and total loss of
    # precision both leave sum_xx - sum_x^2/n at zero. The observed range can,
    # though, and it costs nothing: MIN and MAX come back from the same query
    # and are computed independently of the sums. Identical extremes mean the
    # data really was constant; differing extremes mean the values differed and
    # the arithmetic lost them.
    observed_range = stats.domain_range
    if observed_range is not None and observed_range > 0:
        return (
            "variance came out as exactly zero even though the sampled values "
            f"span {observed_range:g}, so this is not a constant column -- "
            "reconstructing the variance from SUM(x*x) cancelled away every "
            "significant digit, because the column's magnitude dwarfs its "
            "spread. Supply variance_direct from the engine's VAR_SAMP(x) to "
            "recover a real interval here."
        )

    if observed_range is None:
        return (
            "variance came out as exactly zero, which means either the sampled "
            "values were identical or reconstructing the variance from SUM(x*x) "
            "destroyed it. These are indistinguishable without min_x and max_x, "
            "which were not supplied; provide them, or variance_direct, to tell "
            "the two apart."
        )

    rate_bound = rule_of_three_upper_bound(stats.n_domain, coverage_level)
    tail = ""
    if rate_bound is not None:
        tail = (
            f" All {stats.n_domain} sampled values were identical, which bounds "
            f"the share of the population differing from them at {rate_bound:.3%} "
            f"({coverage_level:.0%} confidence) but says nothing about how far "
            "those rows lie from it."
        )
    return (
        "observed sample variance is exactly zero, so no interval is reported; "
        "a constant sample is uninformative, not certain, and a zero-width "
        "interval here would let a caller mistake it for convergence." + tail
    )


# ----------------------------------------------------------------------
# assembly helpers
# ----------------------------------------------------------------------


def _relative_half_width(point: float, half_width: float | None) -> float | None:
    """
    Half-width as a fraction of the estimate.

    Undefined when the estimate is zero -- there is no meaningful relative
    error against zero. Returning None rather than infinity forces callers to
    handle the case explicitly instead of comparing inf against a threshold and
    silently never converging.
    """
    if half_width is None:
        return None
    if point == 0.0:
        return None
    return half_width / abs(point)


def _build_estimate(
    aggregate: Aggregate,
    stats: SampleStats,
    point: float,
    variance: float | None,
    half_width: float | None,
    coverage_level: float,
    method: Method,
    notes: tuple[str, ...],
    *,
    clamp_low: float | None = None,
) -> Estimate:
    """Assemble the output contract, clamping bounds where the domain allows."""
    if half_width is None:
        ci_low: float | None = None
        ci_high: float | None = None
    else:
        ci_low = point - half_width
        ci_high = point + half_width
        if clamp_low is not None and ci_low < clamp_low:
            ci_low = clamp_low

    standard_error = (
        math.sqrt(variance) if variance is not None and variance >= 0 else None
    )

    if not stats.fraction_is_realized:
        notes = notes + (_NOMINAL_FRACTION_NOTE,)

    return Estimate(
        aggregate=aggregate,
        estimate=point,
        variance=variance,
        standard_error=standard_error,
        ci_low=ci_low,
        ci_high=ci_high,
        half_width=half_width,
        relative_half_width=_relative_half_width(point, half_width),
        coverage_level=coverage_level,
        method=method,
        n_sample=stats.n_sample,
        n_domain=stats.n_domain,
        fraction=stats.fraction,
        design_effect=stats.design_effect,
        notes=notes,
    )


def _census_estimate(
    aggregate: Aggregate,
    stats: SampleStats,
    point: float,
    coverage_level: float,
) -> Estimate:
    """f >= 1: the whole relation was read, so the answer is exact."""
    return Estimate(
        aggregate=aggregate,
        estimate=point,
        variance=0.0,
        standard_error=0.0,
        ci_low=point,
        ci_high=point,
        half_width=0.0,
        relative_half_width=0.0 if point != 0 else None,
        coverage_level=coverage_level,
        method=Method.CENSUS,
        n_sample=stats.n_sample,
        n_domain=stats.n_domain,
        fraction=stats.fraction,
        design_effect=stats.design_effect,
        notes=("sampling fraction reached 1.0; result is exact, not estimated",),
    )


def _effective_n(n: float, design_effect: float) -> float:
    """
    Effective sample size under a non-simple design.

    Finite-sample bounds assume independent draws. Block sampling violates that,
    and the standard first-order repair is to deflate n by the design effect.
    Honest but approximate; every estimate built this way is annotated.
    """
    if design_effect <= 1.0:
        return n
    return n / design_effect


def _finite_sample_half_width_on_mean(
    method: Method,
    n: float,
    sample_variance: float | None,
    value_range: float | None,
    coverage_level: float,
) -> float | None:
    """Dispatch to a finite-sample bound on a sample mean, or decline."""
    if value_range is None:
        return None

    if method is Method.HOEFFDING:
        return hoeffding_half_width(n, value_range, coverage_level)

    if method is Method.EMPIRICAL_BERNSTEIN:
        if sample_variance is None:
            return None
        return empirical_bernstein_half_width(
            n, sample_variance, value_range, coverage_level
        )

    return None


# ----------------------------------------------------------------------
# COUNT
# ----------------------------------------------------------------------


def estimate_count(
    stats: SampleStats,
    coverage_level: float = DEFAULT_COVERAGE_LEVEL,
    method: Method = Method.CLT,
    *,
    fallback_to_clt: bool = True,
) -> Estimate:
    """
    Estimate a domain row count.

    Point estimate is the expansion ``n_d / f``. The estimated variance is that
    of a domain total of an indicator::

        p_hat  = n_d / n_s
        s_z^2  = n_s * p_hat * (1 - p_hat) / (n_s - 1)
        Var    = N^2 * (1 - f) / n_s * s_z^2 * DEFF

    Two degenerate cases are handled rather than papered over, because both are
    common in practice and both produce a zero variance estimate that would
    otherwise yield a false zero-width interval:

    * ``n_d == 0`` -- the domain was never observed. That does not prove it is
      empty, so a one-sided rule-of-three upper bound is reported instead of a
      confident zero.
    * ``n_d == n_s`` -- every sampled row was in the domain. Unless the domain
      is declared to be the whole relation, the same argument applies in
      reverse: a rule-of-three bound on the unobserved complement.
    """
    if stats.n_sample == 0:
        return undefined_estimate(
            Aggregate.COUNT, stats, coverage_level, "sample is empty"
        )

    fraction = stats.fraction
    population = stats.population_estimate
    point = stats.n_domain / fraction

    if stats.is_census:
        return _census_estimate(Aggregate.COUNT, stats, float(stats.n_domain), coverage_level)

    # Domain never observed: bound the rate we could have missed.
    if stats.n_domain == 0:
        rate_bound = rule_of_three_upper_bound(stats.n_sample, coverage_level)
        if rate_bound is None:
            return undefined_estimate(
                Aggregate.COUNT, stats, coverage_level, "sample too small", point=0.0
            )
        upper = population * rate_bound * stats.design_effect
        return Estimate(
            aggregate=Aggregate.COUNT,
            estimate=0.0,
            variance=None,
            standard_error=None,
            ci_low=0.0,
            ci_high=upper,
            half_width=upper,
            relative_half_width=None,
            coverage_level=coverage_level,
            method=Method.RULE_OF_THREE,
            n_sample=stats.n_sample,
            n_domain=0,
            fraction=fraction,
            design_effect=stats.design_effect,
            notes=(
                "domain never observed in the sample; zero is the point estimate "
                "but the true count is only bounded above, one-sided",
            ),
        )

    # Whole sample in the domain: bound the unobserved complement.
    if stats.n_domain == stats.n_sample and not stats.domain_is_universe:
        rate_bound = rule_of_three_upper_bound(stats.n_sample, coverage_level)
        if rate_bound is not None:
            half = population * rate_bound * stats.design_effect
            return Estimate(
                aggregate=Aggregate.COUNT,
                estimate=point,
                variance=None,
                standard_error=None,
                ci_low=max(0.0, point - half),
                ci_high=point,
                half_width=half,
                relative_half_width=_relative_half_width(point, half),
                coverage_level=coverage_level,
                method=Method.RULE_OF_THREE,
                n_sample=stats.n_sample,
                n_domain=stats.n_domain,
                fraction=fraction,
                design_effect=stats.design_effect,
                notes=(
                    "every sampled row fell in the domain, so the sample variance "
                    "is zero; bounded below by a rule-of-three argument on the "
                    "unobserved complement rather than reported as exact",
                ),
            )

    if stats.domain_is_universe and stats.n_domain == stats.n_sample:
        return Estimate(
            aggregate=Aggregate.COUNT,
            estimate=population,
            variance=0.0,
            standard_error=0.0,
            ci_low=population,
            ci_high=population,
            half_width=0.0,
            relative_half_width=0.0,
            coverage_level=coverage_level,
            method=Method.CENSUS,
            n_sample=stats.n_sample,
            n_domain=stats.n_domain,
            fraction=fraction,
            design_effect=stats.design_effect,
            notes=(
                "unfiltered COUNT(*) recovers the known population size exactly; "
                "no sampling error",
            ),
        )

    if stats.n_sample < 2:
        return undefined_estimate(
            Aggregate.COUNT,
            stats,
            coverage_level,
            "need at least 2 sampled rows for a variance estimate",
            point=point,
        )

    rate = stats.domain_rate
    indicator_variance = stats.n_sample * rate * (1.0 - rate) / (stats.n_sample - 1)
    variance = (
        (population ** 2)
        * stats.finite_population_correction
        / stats.n_sample
        * indicator_variance
        * stats.design_effect
    )

    # The indicator is observed once per sampled row, so the whole sample
    # carries the degrees of freedom -- always large in practice.
    degrees_of_freedom = stats.n_sample - 1

    notes: tuple[str, ...] = ()
    resolved_method = method
    half_width: float | None

    if method is Method.CLT:
        half_width = clt_half_width(variance, coverage_level, degrees_of_freedom)
    else:
        n_eff = _effective_n(stats.n_sample, stats.design_effect)
        mean_half = _finite_sample_half_width_on_mean(
            method, n_eff, indicator_variance, 1.0, coverage_level
        )
        if mean_half is None:
            if not fallback_to_clt:
                return undefined_estimate(
                    Aggregate.COUNT,
                    stats,
                    coverage_level,
                    f"{method.value} bound unavailable for this cell",
                    point=point,
                )
            resolved_method = Method.CLT
            half_width = clt_half_width(variance, coverage_level, degrees_of_freedom)
            notes = notes + (f"{method.value} unavailable; fell back to CLT",)
        else:
            half_width = population * mean_half
            notes = notes + (_NO_FPC_NOTE,)
            if stats.design_effect > 1.0:
                notes = notes + (_DEFF_HEURISTIC_NOTE,)

    return _build_estimate(
        Aggregate.COUNT,
        stats,
        point,
        variance,
        half_width,
        coverage_level,
        resolved_method,
        notes,
        clamp_low=0.0,
    )


# ----------------------------------------------------------------------
# SUM
# ----------------------------------------------------------------------


def estimate_sum(
    stats: SampleStats,
    coverage_level: float = DEFAULT_COVERAGE_LEVEL,
    method: Method = Method.CLT,
    *,
    fallback_to_clt: bool = True,
) -> Estimate:
    """
    Estimate a domain total of a column.

    Point estimate is ``sum_x / f``. The variance is that of an expanded total::

        s_y^2 = variance of y = x * 1[in domain], taken over all n_s rows
        Var   = N^2 * (1 - f) / n_s * s_y^2 * DEFF

    Because the expanded variable is observed n_s times, this remains
    computable even when only a single row of the domain was sampled.
    """
    if stats.n_sample == 0:
        return undefined_estimate(
            Aggregate.SUM, stats, coverage_level, "sample is empty"
        )

    fraction = stats.fraction
    population = stats.population_estimate
    point = stats.sum_x / fraction

    if stats.is_census:
        return _census_estimate(Aggregate.SUM, stats, stats.sum_x, coverage_level)

    if stats.n_domain == 0:
        return undefined_estimate(
            Aggregate.SUM,
            stats,
            coverage_level,
            "domain never observed in the sample; the total is estimated as zero "
            "but no interval can be formed without an observed value range",
            point=0.0,
        )

    expanded_variance = stats.expanded_variance
    if expanded_variance is None:
        return undefined_estimate(
            Aggregate.SUM,
            stats,
            coverage_level,
            "need at least 2 sampled rows for a variance estimate",
            point=point,
        )

    variance = (
        (population ** 2)
        * stats.finite_population_correction
        / stats.n_sample
        * expanded_variance
        * stats.design_effect
    )

    # The expanded variable y is observed once per sampled row, including the
    # zeros outside the domain, so the whole sample carries the freedom.
    degrees_of_freedom = stats.n_sample - 1

    if expanded_variance == 0.0:
        return undefined_estimate(
            Aggregate.SUM,
            stats,
            coverage_level,
            _degenerate_reason(stats, coverage_level),
            point=point,
            method=Method.DEGENERATE,
        )

    notes: tuple[str, ...] = _precision_notes(stats)
    resolved_method = method
    half_width: float | None

    if method is Method.CLT:
        half_width = clt_half_width(variance, coverage_level, degrees_of_freedom)
    else:
        n_eff = _effective_n(stats.n_sample, stats.design_effect)
        mean_half = _finite_sample_half_width_on_mean(
            method, n_eff, expanded_variance, stats.expanded_range, coverage_level
        )
        if mean_half is None:
            if not fallback_to_clt:
                return undefined_estimate(
                    Aggregate.SUM,
                    stats,
                    coverage_level,
                    f"{method.value} bound needs min_x/max_x, which were not supplied",
                    point=point,
                )
            resolved_method = Method.CLT
            half_width = clt_half_width(variance, coverage_level, degrees_of_freedom)
            notes = notes + (f"{method.value} unavailable; fell back to CLT",)
        else:
            half_width = population * mean_half
            notes = notes + (_NO_FPC_NOTE,)
            if stats.design_effect > 1.0:
                notes = notes + (_DEFF_HEURISTIC_NOTE,)

    # A total of a non-negative column cannot itself be negative.
    clamp_low = 0.0 if stats.min_x is not None and stats.min_x >= 0 else None

    return _build_estimate(
        Aggregate.SUM,
        stats,
        point,
        variance,
        half_width,
        coverage_level,
        resolved_method,
        notes,
        clamp_low=clamp_low,
    )


# ----------------------------------------------------------------------
# AVG
# ----------------------------------------------------------------------


def estimate_avg(
    stats: SampleStats,
    coverage_level: float = DEFAULT_COVERAGE_LEVEL,
    method: Method = Method.CLT,
    *,
    fallback_to_clt: bool = True,
) -> Estimate:
    """
    Estimate a domain mean, as a ratio estimator.

    Point estimate is ``sum_x / n_d``; the sampling fraction cancels, which is
    why AVG needs no scaling. The variance comes from delta-method
    linearization of ``R = Y / Z`` on residuals ``r_i = y_i - R * z_i``::

        SS   = sum_xx - sum_x^2 / n_d          (residual sum of squares)
        Var  = (1 - f) * n_s * SS / (n_d^2 * (n_s - 1)) * DEFF

    which tends to ``(1 - f) * s_x^2 / n_d`` as both n grow, and differs from
    it noticeably for small groups.
    """
    if stats.n_sample == 0:
        return undefined_estimate(
            Aggregate.AVG, stats, coverage_level, "sample is empty"
        )

    if stats.n_domain == 0:
        return undefined_estimate(
            Aggregate.AVG,
            stats,
            coverage_level,
            "domain never observed in the sample; a mean over an empty domain is "
            "undefined and must not be reported as zero",
        )

    point = stats.sum_x / stats.n_domain

    if stats.is_census:
        return _census_estimate(Aggregate.AVG, stats, point, coverage_level)

    if stats.n_domain < 2 or stats.n_sample < 2:
        return undefined_estimate(
            Aggregate.AVG,
            stats,
            coverage_level,
            "need at least 2 domain observations for a variance estimate",
            point=point,
        )

    residual_sum_squares = stats.domain_sum_of_squares
    assert residual_sum_squares is not None

    if residual_sum_squares == 0.0:
        return undefined_estimate(
            Aggregate.AVG,
            stats,
            coverage_level,
            _degenerate_reason(stats, coverage_level),
            point=point,
            method=Method.DEGENERATE,
        )

    variance = (
        stats.finite_population_correction
        * stats.n_sample
        * residual_sum_squares
        / ((stats.n_domain ** 2) * (stats.n_sample - 1))
        * stats.design_effect
    )

    # Only the domain rows inform the residual variance, so a small group gets
    # a small df and a correspondingly wider t multiplier. This is the case the
    # t correction exists for: a 40-row group in a 60M-row table is still a
    # 40-row sample.
    degrees_of_freedom = stats.n_domain - 1

    notes: tuple[str, ...] = _precision_notes(stats)
    resolved_method = method
    half_width: float | None

    if stats.n_domain < 30:
        notes = notes + (
            f"only {stats.n_domain} domain observations; the t multiplier "
            "corrects for the estimated variance, but nothing here corrects for "
            "skew, and a skewed column at this group size will still under-cover "
            "-- prefer a finite-sample method for this cell",
        )


    if method is Method.CLT:
        half_width = clt_half_width(variance, coverage_level, degrees_of_freedom)
    else:
        n_eff = _effective_n(stats.n_domain, stats.design_effect)
        mean_half = _finite_sample_half_width_on_mean(
            method, n_eff, stats.domain_variance, stats.domain_range, coverage_level
        )
        if mean_half is None:
            if not fallback_to_clt:
                return undefined_estimate(
                    Aggregate.AVG,
                    stats,
                    coverage_level,
                    f"{method.value} bound needs min_x/max_x, which were not supplied",
                    point=point,
                )
            resolved_method = Method.CLT
            half_width = clt_half_width(variance, coverage_level, degrees_of_freedom)
            notes = notes + (f"{method.value} unavailable; fell back to CLT",)
        else:
            half_width = mean_half
            notes = notes + (
                _NO_FPC_NOTE,
                "finite-sample bound applied conditionally on the observed domain "
                "size, which is itself random; an approximation for AVG",
            )
            if stats.design_effect > 1.0:
                notes = notes + (_DEFF_HEURISTIC_NOTE,)

    return _build_estimate(
        Aggregate.AVG,
        stats,
        point,
        variance,
        half_width,
        coverage_level,
        resolved_method,
        notes,
    )


# ----------------------------------------------------------------------
# dispatch
# ----------------------------------------------------------------------

_ESTIMATORS = {
    Aggregate.COUNT: estimate_count,
    Aggregate.SUM: estimate_sum,
    Aggregate.AVG: estimate_avg,
}


def estimate_aggregate(
    aggregate: Aggregate,
    stats: SampleStats,
    coverage_level: float = DEFAULT_COVERAGE_LEVEL,
    method: Method = Method.CLT,
    *,
    fallback_to_clt: bool = True,
) -> Estimate:
    """Estimate one aggregate over one domain."""
    try:
        estimator = _ESTIMATORS[aggregate]
    except KeyError:
        raise ValueError(f"unsupported aggregate: {aggregate}") from None
    return estimator(
        stats, coverage_level, method, fallback_to_clt=fallback_to_clt
    )
