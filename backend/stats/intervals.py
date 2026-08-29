"""
Confidence interval methods.

Three methods, deliberately kept side by side so the paper can report the
trade-off rather than asserting one is correct:

    CLT                  Tight, asymptotic. The practical default. Can
                         under-cover for small n, small groups, or heavy skew
                         -- exactly the regimes AetherQuery hits on TPC-H
                         revenue columns.

    Empirical Bernstein  Maurer & Pontil (2009). A genuine finite-sample bound
                         that still uses the observed variance, so it is far
                         tighter than Hoeffding while remaining valid at every
                         n. The honest choice when a guarantee is claimed.

    Hoeffding            Range-only finite-sample bound. Ignores the variance
                         entirely, so it is usually far too loose to be useful
                         on skewed data. Included as the conservative baseline
                         and because prior AQP work cites it.

Everything here bounds the error of a *sample mean*; estimators.py scales those
bounds to population totals. All functions return a non-negative half-width, or
None when the method's preconditions are not met (unknown support, too few
observations). None means "this method declines", and the caller must not
substitute a fabricated value.
"""

from __future__ import annotations

import math
from functools import lru_cache
from statistics import NormalDist

from backend.stats.contracts import Correction

_STANDARD_NORMAL = NormalDist()

# Above this many degrees of freedom the t and normal quantiles agree to well
# under a tenth of a percent, so the cheaper normal quantile is used.
_LARGE_DF = 2000.0


def normal_quantile(coverage_level: float) -> float:
    """
    Two-sided z multiplier for a coverage level.

    ``normal_quantile(0.95)`` is 1.9600, ``normal_quantile(0.99)`` is 2.5758.
    Computed rather than looked up in a table so that multiplicity-adjusted
    levels (which are never round numbers) work correctly.
    """
    if not 0.0 < coverage_level < 1.0:
        raise ValueError("coverage_level must lie strictly in (0, 1)")
    alpha = 1.0 - coverage_level
    return _STANDARD_NORMAL.inv_cdf(1.0 - alpha / 2.0)


# ----------------------------------------------------------------------
# Student's t
#
# The variance in a confidence interval is estimated from the sample, not
# known, and the normal quantile ignores that. For a group with 60 sampled
# rows the correct multiplier is t(0.975, 59) = 2.0010 rather than z = 1.9600,
# and using z produces intervals about 2% too narrow -- which measurably
# under-covers. AetherQuery hits small n constantly, since every small GROUP BY
# group is a small sample regardless of how large the table is.
#
# No scipy here (this package is stdlib-only by design), so the regularized
# incomplete beta function is implemented directly.
# ----------------------------------------------------------------------


def _beta_continued_fraction(a: float, b: float, x: float) -> float:
    """Lentz's algorithm for the continued fraction of the incomplete beta."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0

    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d

    for m in range(1, 301):
        m2 = 2 * m

        numerator = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c

        numerator = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + numerator * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + numerator / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta

        if abs(delta - 1.0) < 3e-16:
            break

    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """I_x(a, b), used here to evaluate the Student t distribution function."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0

    log_front = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    front = math.exp(log_front)

    # The continued fraction converges quickly only on one side of this point;
    # the symmetry I_x(a,b) = 1 - I_(1-x)(b,a) covers the other side.
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_continued_fraction(a, b, x) / a
    return 1.0 - front * _beta_continued_fraction(b, a, 1.0 - x) / b


def student_t_cdf(t: float, df: float) -> float:
    """Distribution function of Student's t with ``df`` degrees of freedom."""
    if df <= 0:
        raise ValueError("df must be positive")
    x = df / (df + t * t)
    tail = 0.5 * regularized_incomplete_beta(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def _cornish_fisher_t(z: float, df: float) -> float:
    """
    Cornish-Fisher expansion of the t quantile about the normal quantile.

    Accurate to roughly 1e-5 for df >= 10, which makes it an excellent starting
    point for the Newton refinement below.
    """
    z2 = z * z
    z3, z5, z7 = z2 * z, z2 * z2 * z, z2 * z2 * z2 * z
    return (
        z
        + (z3 + z) / (4.0 * df)
        + (5.0 * z5 + 16.0 * z3 + 3.0 * z) / (96.0 * df * df)
        + (3.0 * z7 + 19.0 * z5 + 17.0 * z3 - 15.0 * z) / (384.0 * df ** 3)
    )


@lru_cache(maxsize=4096)
def student_t_quantile(coverage_level: float, df: float) -> float:
    """
    Two-sided t multiplier for a coverage level at ``df`` degrees of freedom.

    Falls back to the normal quantile for large df, where the two agree.
    Solved by Newton iteration from a Cornish-Fisher seed, with a bisection
    safeguard so a bad step cannot leave the bracketing interval.
    """
    if not 0.0 < coverage_level < 1.0:
        raise ValueError("coverage_level must lie strictly in (0, 1)")
    if df <= 0:
        raise ValueError("df must be positive")

    z = normal_quantile(coverage_level)
    if df >= _LARGE_DF:
        return z

    target = 1.0 - (1.0 - coverage_level) / 2.0  # upper-tail probability
    log_pdf_constant = (
        math.lgamma((df + 1.0) / 2.0)
        - math.lgamma(df / 2.0)
        - 0.5 * math.log(df * math.pi)
    )

    def pdf(t: float) -> float:
        return math.exp(
            log_pdf_constant - ((df + 1.0) / 2.0) * math.log1p(t * t / df)
        )

    low, high = 0.0, max(1e4, z * 100.0)
    t = max(low + 1e-9, min(high, _cornish_fisher_t(z, df)))

    for _ in range(100):
        error = student_t_cdf(t, df) - target
        if abs(error) < 1e-13:
            break
        if error > 0:
            high = t
        else:
            low = t
        density = pdf(t)
        step = t - error / density if density > 1e-300 else t
        # Keep Newton inside the bracket; otherwise bisect.
        t = step if low < step < high else 0.5 * (low + high)

    return t


def clt_half_width(
    estimator_variance: float,
    coverage_level: float,
    degrees_of_freedom: float | None = None,
) -> float | None:
    """
    Normal-approximation half-width from an estimator's variance.

    The variance passed in is expected to already carry the finite population
    correction and the design effect, since both are properties of the sampling
    design rather than of this interval method.

    When ``degrees_of_freedom`` is supplied the Student t multiplier is used
    instead of z, accounting for the variance itself having been estimated.
    This matters for small GROUP BY groups and is negligible for large ones.
    """
    if estimator_variance < 0 or not math.isfinite(estimator_variance):
        return None
    if degrees_of_freedom is not None and degrees_of_freedom >= 1.0:
        multiplier = student_t_quantile(coverage_level, float(degrees_of_freedom))
    else:
        multiplier = normal_quantile(coverage_level)
    return multiplier * math.sqrt(estimator_variance)


def empirical_bernstein_half_width(
    n: float,
    sample_variance: float,
    value_range: float,
    coverage_level: float,
) -> float | None:
    """
    Maurer-Pontil empirical Bernstein bound on the error of a sample mean.

    With probability at least ``1 - delta``::

        |mean_hat - mu| <= sqrt(2 * V_n * ln(3/delta) / n)
                           + 3 * R * ln(3/delta) / (n - 1)

    where ``V_n`` is the unbiased sample variance and ``R`` the width of the
    variable's support. The first term is the Bernstein/variance term and
    shrinks like ``1/sqrt(n)``; the second is the range penalty and shrinks
    like ``1/n``, so it is only material at small n.

    Validity notes
    --------------
    * The theorem assumes i.i.d. draws (sampling with replacement). Sampling
      *without* replacement is more concentrated than with replacement
      (Hoeffding 1963), so the bound remains valid for our without-replacement
      sampling -- it is conservative, and in particular it does not get the
      benefit of the finite population correction. That is a real cost at high
      sampling fractions and is reported as such rather than papered over.
    * The i.i.d. assumption fails outright under block/cluster sampling.
      estimators.py handles that by substituting an effective sample size
      ``n / DEFF``, which is a heuristic, not a theorem, and is flagged in the
      resulting Estimate's notes.

    Reference: Maurer & Pontil, "Empirical Bernstein Bounds and Sample
    Variance Penalization", COLT 2009, Theorem 4.
    """
    if n < 2:
        return None
    if value_range < 0 or not math.isfinite(value_range):
        return None
    if sample_variance < 0 or not math.isfinite(sample_variance):
        return None

    delta = 1.0 - coverage_level
    if not 0.0 < delta < 1.0:
        raise ValueError("coverage_level must lie strictly in (0, 1)")

    log_term = math.log(3.0 / delta)
    variance_term = math.sqrt(2.0 * sample_variance * log_term / n)
    range_term = 3.0 * value_range * log_term / (n - 1)
    return variance_term + range_term


def hoeffding_half_width(
    n: float,
    value_range: float,
    coverage_level: float,
) -> float | None:
    """
    Hoeffding bound on the error of a sample mean.

    With probability at least ``1 - delta``::

        |mean_hat - mu| <= R * sqrt(ln(2/delta) / (2n))

    Uses only the support width, never the observed variance, which is why it
    is so loose on low-variance data. Like the Bernstein bound it holds for
    sampling without replacement (Hoeffding 1963) and does not benefit from the
    finite population correction.
    """
    if n < 1:
        return None
    if value_range < 0 or not math.isfinite(value_range):
        return None

    delta = 1.0 - coverage_level
    if not 0.0 < delta < 1.0:
        raise ValueError("coverage_level must lie strictly in (0, 1)")

    return value_range * math.sqrt(math.log(2.0 / delta) / (2.0 * n))


def rule_of_three_upper_bound(n: int, coverage_level: float = 0.95) -> float | None:
    """
    Upper bound on a rate after observing zero events in ``n`` trials.

    Seeing no rows of a domain in the sample does not prove the domain is
    empty. If the true rate were p, the chance of missing it entirely is
    ``(1-p)^n``; setting that equal to ``1 - coverage_level`` and solving gives
    ``p <= -ln(1 - coverage) / n``, which is the familiar ``3/n`` at 95%.

    Returns a bound on the *rate*; the caller multiplies by N for a count.
    """
    if n < 1:
        return None
    if not 0.0 < coverage_level < 1.0:
        raise ValueError("coverage_level must lie strictly in (0, 1)")
    return -math.log(1.0 - coverage_level) / n


def adjust_coverage_level(
    coverage_level: float,
    num_intervals: int,
    correction: Correction = Correction.BONFERRONI,
) -> float:
    """
    Raise a per-interval coverage level so a whole family covers simultaneously.

    A GROUP BY over 25 nations with 3 aggregates produces 75 intervals. Each
    holding with probability 0.95 individually does *not* mean all 75 hold with
    probability 0.95 -- under independence that would be 0.95^75, about 2%. A
    paper claiming "our intervals cover" has to say which of the two it means.

    Bonferroni splits the error budget evenly (``alpha / m``); it needs no
    independence assumption and is the safe default. Sidak
    (``1 - (1-alpha)^(1/m)``) is slightly tighter but assumes independence
    across intervals, which grouped aggregates over a shared sample do not
    strictly satisfy.
    """
    if num_intervals < 1:
        raise ValueError("num_intervals must be at least 1")
    if not 0.0 < coverage_level < 1.0:
        raise ValueError("coverage_level must lie strictly in (0, 1)")

    if correction is Correction.NONE or num_intervals == 1:
        return coverage_level

    alpha = 1.0 - coverage_level

    if correction is Correction.BONFERRONI:
        return 1.0 - alpha / num_intervals

    if correction is Correction.SIDAK:
        return (1.0 - alpha) ** (1.0 / num_intervals)

    raise ValueError(f"unknown correction: {correction}")
