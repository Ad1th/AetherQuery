"""
Coverage tests: does a nominal 95% interval actually cover 95% of the time?

This is the gate that certifies the estimator library. The unit tests in
test_stats_estimators.py check that the algebra is right; these check that the
resulting intervals make true statements about the world.

Every assertion goes through ``CoverageResult.verdict``, which compares the
Wilson interval around the *measured* coverage against nominal rather than
comparing point estimates. That matters: at these trial counts a measured 94.2%
is not distinguishable from 95%, and a test that failed on it would be flaky by
construction. The verdict is one-sided -- over-covering is conservative and
therefore safe, only under-coverage falsifies the claim.

Trial counts here are kept low enough for the suite to stay fast. The full
study, with the trial counts that resolve borderline cases, is
``scripts/run_coverage_study.py``.

Several tests deliberately assert that coverage *fails*. Those lock in measured
findings -- skew breaks the normal approximation, and reconstructing a variance
from a pushed-down sum of squares breaks on large-magnitude columns. If one of
them starts passing, the finding has changed and the paper text that depends on
it needs revisiting.
"""

from __future__ import annotations

import pytest

from backend.stats.contracts import Aggregate, Correction, Method, SampleStats
from backend.stats.simulation import (
    bernoulli,
    constant_population,
    lognormal_population,
    normal_population,
    offset_population,
    pareto_population,
    run_coverage_study,
    run_simultaneous_coverage_study,
    srswor,
    wilson_interval,
    zero_inflated_population,
)

TRIALS = 800
SIZE = 12_000
FRACTION = 0.10


# ----------------------------------------------------------------------
# the harness itself
# ----------------------------------------------------------------------


class TestWilsonInterval:
    """The verdicts depend on this, so it is checked first."""

    def test_brackets_the_point_estimate(self):
        low, high = wilson_interval(950, 1000)
        assert low < 0.95 < high

    def test_narrows_as_trials_grow(self):
        narrow = wilson_interval(9500, 10_000)
        wide = wilson_interval(95, 100)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])

    def test_never_exceeds_unit_interval(self):
        low, high = wilson_interval(100, 100)
        assert low >= 0.0 and high <= 1.0

    def test_handles_zero_trials(self):
        assert wilson_interval(0, 0) == (0.0, 1.0)


# ----------------------------------------------------------------------
# calibration: symmetric populations must cover
# ----------------------------------------------------------------------


class TestSymmetricPopulationsCover:
    """
    A symmetric, light-tailed population is the case the theory is built for.
    Under-coverage here is a defect, not a finding, so these are the tests that
    would catch a wrong variance formula.
    """

    @pytest.mark.parametrize(
        "aggregate", [Aggregate.COUNT, Aggregate.SUM, Aggregate.AVG]
    )
    def test_normal_population(self, aggregate):
        result = run_coverage_study(
            normal_population(size=SIZE),
            aggregate,
            fraction=FRACTION,
            trials=TRIALS,
        )
        assert result.verdict == "PASS", (
            f"{aggregate.value} covered {result.empirical:.2%}, "
            f"Wilson {result.coverage_interval}"
        )

    def test_bernoulli_sampling_also_covers(self):
        """The estimators should not be sensitive to which row-level design is used."""
        result = run_coverage_study(
            normal_population(size=SIZE),
            Aggregate.SUM,
            fraction=FRACTION,
            trials=TRIALS,
            sampler=bernoulli,
        )
        assert result.verdict == "PASS"

    def test_high_sampling_fraction_covers(self):
        """Exercises the finite population correction, which dominates here."""
        result = run_coverage_study(
            normal_population(size=SIZE),
            Aggregate.SUM,
            fraction=0.80,
            trials=TRIALS,
        )
        assert result.verdict == "PASS"

    def test_count_covers_even_on_heavy_tails(self):
        """
        COUNT estimates an indicator mean, which is bounded and mildly skewed
        however wild the underlying column is. It should be robust where SUM
        and AVG are not.
        """
        result = run_coverage_study(
            pareto_population(size=SIZE, alpha=1.8),
            Aggregate.COUNT,
            fraction=FRACTION,
            trials=TRIALS,
        )
        assert result.verdict == "PASS"

    def test_zero_inflated_covers(self):
        result = run_coverage_study(
            zero_inflated_population(size=SIZE),
            Aggregate.SUM,
            fraction=FRACTION,
            trials=TRIALS,
        )
        assert result.verdict == "PASS"


# ----------------------------------------------------------------------
# finite-sample methods must be conservative, never optimistic
# ----------------------------------------------------------------------


class TestFiniteSampleMethodsAreConservative:
    @pytest.mark.parametrize(
        "method", [Method.EMPIRICAL_BERNSTEIN, Method.HOEFFDING]
    )
    @pytest.mark.parametrize("aggregate", [Aggregate.SUM, Aggregate.AVG])
    def test_never_under_covers_even_on_heavy_tails(self, method, aggregate):
        """
        These bounds hold at every sample size and make no distributional
        assumption, so they must survive the population that breaks the CLT.
        """
        result = run_coverage_study(
            pareto_population(size=SIZE, alpha=1.8),
            aggregate,
            fraction=FRACTION,
            trials=TRIALS,
            method=method,
        )
        assert result.verdict == "PASS"
        assert result.empirical >= 0.95

    def test_conservatism_is_paid_for_in_width(self):
        """
        Guarantees are not free, and the cost should be visible rather than
        assumed. If Bernstein ever stops being wider than CLT, one of them is
        wrong.
        """
        population = lognormal_population(size=SIZE, sigma=0.9)
        clt = run_coverage_study(
            population, Aggregate.AVG, fraction=FRACTION, trials=200
        )
        bernstein = run_coverage_study(
            population,
            Aggregate.AVG,
            fraction=FRACTION,
            trials=200,
            method=Method.EMPIRICAL_BERNSTEIN,
        )
        assert bernstein.mean_relative_half_width > clt.mean_relative_half_width


# ----------------------------------------------------------------------
# recorded findings: these assert that coverage FAILS
# ----------------------------------------------------------------------


class TestRecordedFindings:
    """
    Regression locks on measured negative results. A failure here means the
    finding changed, which is a reason to re-examine the claim rather than to
    "fix" the test.
    """

    def test_skew_breaks_the_normal_approximation(self):
        """
        Heavy right skew makes the CLT under-cover for AVG even at a sample
        size where a symmetric population is comfortably calibrated. This is
        the finding that justifies keeping the finite-sample methods.
        """
        result = run_coverage_study(
            pareto_population(size=SIZE, alpha=1.8),
            Aggregate.AVG,
            fraction=0.05,
            trials=2000,
        )
        assert result.verdict == "UNDER"
        assert result.empirical < 0.94

    def test_pushdown_variance_breaks_on_large_magnitude_columns(self):
        """
        Reconstructing the variance as sum_xx - sum_x^2/n destroys precision
        when the column's mean dwarfs its spread.

        Since the degenerate-variance guard landed, the failure presents as
        abstention rather than as silent under-coverage: total cancellation
        gives exactly zero, which the guard catches, so roughly half of all
        trials now decline to produce an interval instead of producing a wrong
        one. That is a better failure, but it is still a failure -- half the
        answers are lost and the remainder are badly miscalibrated.
        """
        result = run_coverage_study(
            offset_population(size=SIZE, offset=1e9, stdev=1.0),
            Aggregate.AVG,
            fraction=FRACTION,
            trials=TRIALS,
        )
        assert result.resolution_rate < 0.75, (
            f"expected most trials to be refused, got "
            f"{result.resolution_rate:.0%} resolved"
        )

    def test_engine_supplied_variance_repairs_it(self):
        """The same configuration, with VAR_SAMP supplied instead."""
        result = run_coverage_study(
            offset_population(size=SIZE, offset=1e9, stdev=1.0),
            Aggregate.AVG,
            fraction=FRACTION,
            trials=TRIALS,
            stable_variance=True,
        )
        assert result.verdict == "PASS"

    def test_precision_loss_is_detected_not_just_suffered(self):
        """The library must say so, rather than silently returning the interval."""
        stats = SampleStats(
            n_sample=1000,
            n_domain=1000,
            sum_x=1e9 * 1000,
            sum_xx=(1e9 ** 2) * 1000,
            min_x=1e9 - 1,
            max_x=1e9 + 1,
            population_size=100_000,
        )
        assert stats.variance_precision_is_degraded

    def test_healthy_columns_are_not_falsely_flagged(self):
        stats = SampleStats(
            n_sample=1000,
            n_domain=400,
            sum_x=400 * 50.0,
            sum_xx=400 * (2500.0 + 144.0),
            min_x=10.0,
            max_x=90.0,
            population_size=100_000,
        )
        assert not stats.variance_precision_is_degraded

    def test_constant_column_produces_no_intervals_at_all(self):
        """
        A constant column used to yield a zero-width interval that trivially
        contained the truth and scored a flattering 100%. The estimators now
        refuse outright, so the harness sees no usable intervals and abstains --
        which is the honest outcome, since nothing was measured.
        """
        result = run_coverage_study(
            constant_population(size=SIZE),
            Aggregate.AVG,
            fraction=FRACTION,
            trials=200,
        )
        assert result.resolved == 0
        assert result.verdict == "ABSTAIN"


# ----------------------------------------------------------------------
# multiplicity
# ----------------------------------------------------------------------


class TestSimultaneousCoverage:
    """
    The headline multiplicity result: sixteen intervals each individually at
    95% do not hold together at anything close to 95%.
    """

    @staticmethod
    def _study(correction, trials=600):
        from backend.stats.simulation import grouped_population

        return run_simultaneous_coverage_study(
            grouped_population(size=20_000, num_groups=8),
            aggregates=(Aggregate.COUNT, Aggregate.SUM),
            fraction=FRACTION,
            trials=trials,
            correction=correction,
        )

    def test_uncorrected_intervals_do_not_hold_together(self):
        result = self._study(Correction.NONE)
        assert result.marginal >= 0.93, "per-interval coverage should still be fine"
        assert result.simultaneous < 0.85, (
            "family-wise coverage should collapse without a correction; "
            f"got {result.simultaneous:.2%}"
        )
        assert result.verdict == "UNDER"

    def test_bonferroni_restores_family_wise_coverage(self):
        result = self._study(Correction.BONFERRONI)
        assert result.verdict == "PASS", (
            f"simultaneous coverage {result.simultaneous:.2%}, "
            f"Wilson {result.coverage_interval}"
        )

    def test_sidak_restores_family_wise_coverage(self):
        result = self._study(Correction.SIDAK)
        assert result.verdict == "PASS"

    def test_correction_costs_width(self):
        uncorrected = self._study(Correction.NONE, trials=200)
        corrected = self._study(Correction.BONFERRONI, trials=200)
        assert (
            corrected.mean_relative_half_width
            > uncorrected.mean_relative_half_width
        )
