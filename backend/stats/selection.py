"""
Choosing an interval method from the data rather than by hand.

The coverage study established that skew, not sample size, is what breaks the
normal approximation. SUM coverage ran 95.4% on a normal population, 93.7% on
a lognormal one and 92.7% on a Pareto one at identical sample sizes, while
twelve Zipf-sized groups down to n=45 all covered fine on near-symmetric data.
So "use a finite-sample method for small groups" was the wrong rule; the right
one keys on skew.

Cochran's rule of thumb makes that decidable: a normal-approximation interval
on a mean needs roughly

    n >= k * g1^2

where g1 is the sample skewness. A symmetric column satisfies it almost
immediately; a skewed one needs quadratically more.

Why k is 100 here and not the textbook 25
------------------------------------------
The rule was checked against the coverage simulator rather than adopted on
authority, and the textbook constant did not survive the check. Treating the
rule as a classifier over 42 configurations -- seven populations, three
sampling fractions, SUM and AVG -- and comparing its prediction against
measured coverage:

    k = 25    recall  86%   2 configurations under-covered unflagged
    k = 50    recall  93%   1 unflagged
    k = 100   recall 100%   none unflagged, 10 of 28 sound cells flagged anyway
    k = 400   recall 100%   18 of 28 flagged anyway

The two error types are not symmetric. A cell waved through that then
under-covers is a silently wrong answer, which is the failure this whole track
exists to prevent. A cell needlessly routed to a finite-sample bound merely
gets a wider interval. So the constant is set where recall reaches 100%.

The classes do not separate perfectly at any constant -- the worst
under-covering configuration sat at a ratio of 61.9 while the best sound one
sat at 23.3 -- so some over-caution is unavoidable, not a tuning failure.

The textbook value is not wrong so much as aimed at a weaker target: Cochran's
25 concerns the one-sided tail probability of a mean, while what is wanted here
is two-sided coverage within noise of nominal, for totals of zero-inflated
variables as well as means.

COUNT is exempt. It estimates the mean of an indicator, which is bounded in
[0, 1] and cannot be badly skewed; it covered at 94.7-95.9% on every population
tested, including the heavy-tailed ones.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.stats.contracts import Aggregate, Method, SampleStats

COCHRAN_CONSTANT = 100.0
"""
The multiplier in ``n >= k * g1^2``.

Calibrated against measured coverage, not taken from the textbook -- see the
module docstring. Four times the classical 25, which was measurably too lenient
for the two-sided coverage this package promises.
"""


@dataclass(frozen=True)
class MethodRecommendation:
    """
    A method choice, with the evidence behind it.

    ``basis`` distinguishes a decision made from measured skewness from one
    made because skewness was unavailable. That difference matters: the second
    is a default, not a finding, and a caller that wants a guarantee should
    supply the third moment rather than trust it.
    """

    method: Method
    reason: str
    basis: str
    skewness: float | None = None
    required_n: float | None = None

    @property
    def is_measured(self) -> bool:
        return self.basis == "measured"


def recommend_method(
    stats: SampleStats,
    aggregate: Aggregate = Aggregate.AVG,
) -> MethodRecommendation:
    """
    Pick an interval method for one cell.

    Returns CLT when the normal approximation is defensible for this data, and
    empirical Bernstein when it is not. Bernstein needs bounded support, so
    when the range is unknown the recommendation falls back to CLT and says so
    -- the caller then knows it is using an approximation the data does not
    support, which is better than silently getting one.
    """
    if aggregate is Aggregate.COUNT:
        return MethodRecommendation(
            method=Method.CLT,
            reason=(
                "COUNT estimates an indicator mean, which is bounded and cannot "
                "be badly skewed; measured at 94.7-95.9% coverage on every "
                "population tested, including heavy-tailed ones"
            ),
            basis="measured",
        )

    # SUM estimates a total of the expanded variable, so it is that variable's
    # skew that governs the approximation -- and the out-of-domain zeros make
    # it worse than the domain's own. AVG is a mean over the domain, so the
    # domain's skew is the relevant one.
    if aggregate is Aggregate.SUM:
        skewness = stats.expanded_skewness
        effective_n = stats.n_sample
    else:
        skewness = stats.domain_skewness
        effective_n = stats.n_domain

    if skewness is None:
        return MethodRecommendation(
            method=Method.CLT,
            reason=(
                "skewness unavailable, so the normal approximation cannot be "
                "checked; supply sum_xxx or skewness_direct to make this a "
                "decision rather than a default"
            ),
            basis="unverified",
        )

    required_n = COCHRAN_CONSTANT * skewness * skewness

    if effective_n >= required_n:
        return MethodRecommendation(
            method=Method.CLT,
            reason=(
                f"skewness {skewness:+.2f} needs about {required_n:.0f} "
                f"observations for the normal approximation and this cell has "
                f"{effective_n}"
            ),
            basis="measured",
            skewness=skewness,
            required_n=required_n,
        )

    if stats.domain_range is None:
        return MethodRecommendation(
            method=Method.CLT,
            reason=(
                f"skewness {skewness:+.2f} needs about {required_n:.0f} "
                f"observations and this cell has only {effective_n}, so the "
                "interval is expected to under-cover; a finite-sample method "
                "would be preferable but needs min_x and max_x, which were not "
                "supplied"
            ),
            basis="measured",
            skewness=skewness,
            required_n=required_n,
        )

    return MethodRecommendation(
        method=Method.EMPIRICAL_BERNSTEIN,
        reason=(
            f"skewness {skewness:+.2f} needs about {required_n:.0f} observations "
            f"for the normal approximation but this cell has only "
            f"{effective_n}; a finite-sample bound is valid at any size"
        ),
        basis="measured",
        skewness=skewness,
        required_n=required_n,
    )


def observations_needed(skewness: float) -> float:
    """
    How many observations Cochran's rule asks for at a given skew.

    Useful for a controller deciding how far to escalate a sampling
    progression: a column with skew 5 will not produce a trustworthy
    normal-approximation interval until the domain reaches ~625 rows, and no
    amount of extra sampling helps a *group* that is simply small.
    """
    return COCHRAN_CONSTANT * skewness * skewness
