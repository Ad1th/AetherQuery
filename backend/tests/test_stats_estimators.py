"""
Unit tests for the statistical estimator library.

These are fast and deterministic. The heavyweight coverage simulator -- which
repeatedly samples known populations and compares empirical against nominal
coverage -- is separate work; what is verified here is that the estimators are
algebraically correct, that the degenerate cases behave, and that the
interval methods stand in the expected order.

The strongest test in this file is TestUnbiasedness, which enumerates *every*
sample of a small population rather than simulating. Under simple random
sampling without replacement the expansion estimator is exactly unbiased and
its variance estimator is exactly unbiased for the estimator's true variance,
so both can be checked to machine precision instead of to a tolerance.
"""

from __future__ import annotations

import itertools
import math

import pytest

from backend.stats import (
    Aggregate,
    AggregateCell,
    Correction,
    Method,
    SampleStats,
    adjust_coverage_level,
    estimate_avg,
    estimate_count,
    estimate_query,
    estimate_sum,
)
from backend.stats.intervals import (
    normal_quantile,
    rule_of_three_upper_bound,
    student_t_cdf,
    student_t_quantile,
)


# ----------------------------------------------------------------------
# distributional machinery
# ----------------------------------------------------------------------


class TestStudentT:
    """The t quantile is solved numerically, so it is checked against tables."""

    # (coverage, df, published two-sided critical value)
    PUBLISHED = [
        (0.95, 1, 12.7062),
        (0.95, 4, 2.7764),
        (0.95, 9, 2.2622),
        (0.95, 29, 2.0452),
        (0.95, 59, 2.0010),
        (0.99, 4, 4.6041),
        (0.99, 30, 2.7500),
        (0.90, 10, 1.8125),
        (0.999, 20, 3.8495),
    ]

    @pytest.mark.parametrize("coverage,df,expected", PUBLISHED)
    def test_matches_published_tables(self, coverage, df, expected):
        assert student_t_quantile(coverage, float(df)) == pytest.approx(
            expected, abs=1e-4
        )

    def test_converges_to_normal_for_large_df(self):
        assert student_t_quantile(0.95, 5000.0) == pytest.approx(
            normal_quantile(0.95), abs=1e-9
        )

    def test_always_wider_than_normal(self):
        for df in (2, 5, 20, 100, 500):
            assert student_t_quantile(0.95, float(df)) >= normal_quantile(0.95)

    def test_cdf_is_symmetric(self):
        for t in (0.3, 1.0, 2.5):
            assert student_t_cdf(t, 7.0) + student_t_cdf(-t, 7.0) == pytest.approx(1.0)

    def test_quantile_inverts_cdf(self):
        for coverage in (0.80, 0.95, 0.99):
            for df in (3.0, 12.0, 400.0):
                t = student_t_quantile(coverage, df)
                upper_tail = 1.0 - (1.0 - coverage) / 2.0
                assert student_t_cdf(t, df) == pytest.approx(upper_tail, abs=1e-10)


class TestRuleOfThree:
    def test_is_three_over_n_at_95_percent(self):
        assert rule_of_three_upper_bound(1000, 0.95) * 1000 == pytest.approx(
            3.0, abs=0.01
        )

    def test_tightens_as_sample_grows(self):
        assert rule_of_three_upper_bound(100) > rule_of_three_upper_bound(10_000)


# ----------------------------------------------------------------------
# unbiasedness, by exhaustive enumeration
# ----------------------------------------------------------------------


class TestUnbiasedness:
    """
    Enumerate every sample of a small population.

    With N = 8 and n = 5 there are only 56 possible samples, so expectations can
    be computed exactly rather than simulated. Both the point estimator and the
    variance estimator are unbiased under SRSWOR, so both are checked to
    floating-point precision.

    The 4/4 split is deliberate. Sampling 5 of 8 rows guarantees every sample
    contains between 1 and 4 domain rows, so no sample can be degenerate
    (empty domain, or every sampled row in the domain). Those cases divert to
    the rule-of-three path, which reports no variance at all, and including
    them would compare an average taken over one set of samples against a
    variance taken over another.
    """

    POPULATION = [
        (True, 4.0),
        (True, 7.0),
        (True, 2.0),
        (True, 11.0),
        (False, 0.0),
        (False, 0.0),
        (False, 0.0),
        (False, 0.0),
    ]
    N = 8
    N_SAMPLE = 5

    def test_fixture_admits_no_degenerate_samples(self):
        """Guards the assumption the two variance tests below rely on."""
        for stats in self._all_samples():
            assert 0 < stats.n_domain < stats.n_sample

    def _all_samples(self):
        for combo in itertools.combinations(self.POPULATION, self.N_SAMPLE):
            domain = [value for in_domain, value in combo if in_domain]
            yield SampleStats(
                n_sample=self.N_SAMPLE,
                n_domain=len(domain),
                sum_x=sum(domain),
                sum_xx=sum(v * v for v in domain),
                min_x=min(domain) if domain else None,
                max_x=max(domain) if domain else None,
                population_size=self.N,
            )

    def test_count_point_estimator_is_unbiased(self):
        truth = sum(1 for in_domain, _ in self.POPULATION if in_domain)
        estimates = [estimate_count(s).estimate for s in self._all_samples()]
        assert sum(estimates) / len(estimates) == pytest.approx(truth)

    def test_sum_point_estimator_is_unbiased(self):
        truth = sum(v for in_domain, v in self.POPULATION if in_domain)
        estimates = [estimate_sum(s).estimate for s in self._all_samples()]
        assert sum(estimates) / len(estimates) == pytest.approx(truth)

    def test_sum_variance_estimator_is_unbiased(self):
        """E[v_hat] must equal the true sampling variance of the estimator."""
        results = [estimate_sum(s) for s in self._all_samples()]
        points = [r.estimate for r in results]
        variances = [r.variance for r in results]

        mean_point = sum(points) / len(points)
        true_variance = sum((p - mean_point) ** 2 for p in points) / len(points)
        mean_estimated_variance = sum(variances) / len(variances)

        assert mean_estimated_variance == pytest.approx(true_variance, rel=1e-9)

    def test_count_variance_estimator_is_unbiased(self):
        results = [estimate_count(s) for s in self._all_samples()]
        points = [r.estimate for r in results]
        variances = [r.variance for r in results]

        mean_point = sum(points) / len(points)
        true_variance = sum((p - mean_point) ** 2 for p in points) / len(points)
        mean_estimated_variance = sum(variances) / len(variances)

        assert mean_estimated_variance == pytest.approx(true_variance, rel=1e-9)


# ----------------------------------------------------------------------
# finite population correction
# ----------------------------------------------------------------------


class TestFinitePopulationCorrection:
    @staticmethod
    def _stats(fraction: float) -> SampleStats:
        population = 100_000
        n_sample = int(population * fraction)
        n_domain = n_sample // 4
        mean, spread = 20.0, 6.0
        return SampleStats(
            n_sample=n_sample,
            n_domain=n_domain,
            sum_x=n_domain * mean,
            sum_xx=n_domain * (mean * mean + spread * spread),
            min_x=0.0,
            max_x=60.0,
            population_size=population,
        )

    def test_variance_decreases_monotonically_with_fraction(self):
        widths = [
            estimate_sum(self._stats(f)).relative_half_width
            for f in (0.01, 0.05, 0.25, 0.50, 0.90)
        ]
        assert widths == sorted(widths, reverse=True)

    def test_census_has_zero_error(self):
        estimate = estimate_sum(self._stats(1.0))
        assert estimate.method is Method.CENSUS
        assert estimate.variance == 0.0
        assert estimate.ci_low == estimate.ci_high == estimate.estimate

    def test_half_fraction_halves_the_variance_versus_no_correction(self):
        """At f = 0.5 the (1-f) term should remove exactly half the variance."""
        stats = self._stats(0.5)
        uncorrected = (
            (stats.population_estimate ** 2)
            * stats.expanded_variance
            / stats.n_sample
        )
        assert estimate_sum(stats).variance == pytest.approx(uncorrected * 0.5)


# ----------------------------------------------------------------------
# the AVG ratio estimator
# ----------------------------------------------------------------------


class TestRatioEstimator:
    def test_reduces_to_simple_form_for_large_samples(self):
        """Var -> (1 - f) * s^2 / n_d as both sample sizes grow."""
        population, n_sample, n_domain = 10_000_000, 100_000, 10_000
        mean, variance = 50.0, 144.0
        stats = SampleStats(
            n_sample=n_sample,
            n_domain=n_domain,
            sum_x=n_domain * mean,
            sum_xx=n_domain * (mean * mean) + (n_domain - 1) * variance,
            population_size=population,
        )
        expected = stats.finite_population_correction * variance / n_domain
        assert estimate_avg(stats).variance == pytest.approx(expected, rel=1e-3)

    def test_point_estimate_is_the_domain_mean(self):
        stats = SampleStats(
            n_sample=1000,
            n_domain=200,
            sum_x=3000.0,
            sum_xx=48_000.0,
            population_size=50_000,
        )
        assert estimate_avg(stats).estimate == pytest.approx(15.0)

    def test_sampling_fraction_does_not_scale_the_estimate(self):
        """Unlike COUNT and SUM, an average needs no expansion by 1/f."""
        kwargs = dict(n_domain=200, sum_x=3000.0, sum_xx=48_000.0)
        low = estimate_avg(SampleStats(n_sample=1000, population_size=1_000_000, **kwargs))
        high = estimate_avg(SampleStats(n_sample=1000, population_size=2000, **kwargs))
        assert low.estimate == pytest.approx(high.estimate)

    def test_small_group_uses_a_wider_multiplier_than_normal(self):
        stats = SampleStats(
            n_sample=10_000,
            n_domain=12,
            sum_x=120.0,
            sum_xx=1400.0,
            population_size=1_000_000,
        )
        estimate = estimate_avg(stats)
        z_based = normal_quantile(0.95) * math.sqrt(estimate.variance)
        assert estimate.half_width > z_based
        assert any("skew" in note for note in estimate.notes)


# ----------------------------------------------------------------------
# degenerate and edge cases
# ----------------------------------------------------------------------


class TestEdgeCases:
    def test_unobserved_domain_gives_one_sided_count_bound(self):
        stats = SampleStats(n_sample=500, n_domain=0, population_size=100_000)
        estimate = estimate_count(stats)
        assert estimate.method is Method.RULE_OF_THREE
        assert estimate.estimate == 0.0
        assert estimate.ci_low == 0.0
        assert estimate.ci_high > 0.0

    def test_unobserved_domain_gives_no_average(self):
        """A mean over an empty domain is undefined and must not become zero."""
        stats = SampleStats(n_sample=500, n_domain=0, population_size=100_000)
        estimate = estimate_avg(stats)
        assert estimate.estimate is None
        assert estimate.method is Method.UNDEFINED
        assert not estimate.is_usable

    def test_single_domain_observation_still_yields_a_sum_interval(self):
        """The expanded variable is observed n_sample times, not n_domain."""
        stats = SampleStats(
            n_sample=500,
            n_domain=1,
            sum_x=5.0,
            sum_xx=25.0,
            min_x=5.0,
            max_x=5.0,
            population_size=100_000,
        )
        assert estimate_sum(stats).is_usable

    def test_single_domain_observation_yields_no_average_interval(self):
        stats = SampleStats(
            n_sample=500, n_domain=1, sum_x=5.0, sum_xx=25.0, population_size=100_000
        )
        estimate = estimate_avg(stats)
        assert estimate.estimate == pytest.approx(5.0)
        assert estimate.ci_low is None

    def test_zero_valued_estimate_has_no_relative_error(self):
        stats = SampleStats(
            n_sample=500,
            n_domain=100,
            sum_x=0.0,
            sum_xx=0.0,
            min_x=0.0,
            max_x=0.0,
            population_size=100_000,
        )
        estimate = estimate_sum(stats)
        assert estimate.estimate == 0.0
        assert estimate.relative_half_width is None
        assert estimate.meets_target(0.05) is None

    def test_zero_variance_yields_no_interval_at_all(self):
        """
        A constant sample is uninformative, not certain. Reporting the
        zero-width interval the algebra produces would let a controller read
        relative_half_width == 0.0 as instant convergence.
        """
        stats = SampleStats(
            n_sample=500,
            n_domain=500,
            sum_x=2500.0,
            sum_xx=12_500.0,
            min_x=5.0,
            max_x=5.0,
            population_size=100_000,
        )
        estimate = estimate_sum(stats)
        assert estimate.method is Method.DEGENERATE
        assert estimate.ci_low is None
        assert estimate.relative_half_width is None
        assert estimate.estimate is not None

    def test_full_sample_in_domain_is_bounded_not_declared_exact(self):
        stats = SampleStats(n_sample=800, n_domain=800, population_size=100_000)
        estimate = estimate_count(stats)
        assert estimate.method is Method.RULE_OF_THREE
        assert estimate.ci_low < estimate.estimate

    def test_declared_universe_recovers_population_exactly(self):
        stats = SampleStats(
            n_sample=800, n_domain=800, population_size=100_000, domain_is_universe=True
        )
        estimate = estimate_count(stats)
        assert estimate.method is Method.CENSUS
        assert estimate.estimate == 100_000
        assert estimate.half_width == 0.0

    def test_nominal_fraction_is_flagged_as_a_bias_risk(self):
        stats = SampleStats(
            n_sample=1000,
            n_domain=250,
            sum_x=5000.0,
            sum_xx=120_000.0,
            nominal_fraction=0.01,
        )
        assert any("nominal" in note for note in estimate_sum(stats).notes)


class TestValidation:
    def test_domain_cannot_exceed_sample(self):
        with pytest.raises(ValueError, match="cannot exceed"):
            SampleStats(n_sample=10, n_domain=11, population_size=100)

    def test_sample_cannot_exceed_population(self):
        with pytest.raises(ValueError, match="cannot exceed"):
            SampleStats(n_sample=200, n_domain=10, population_size=100)

    def test_fraction_must_be_knowable(self):
        with pytest.raises(ValueError, match="population_size"):
            SampleStats(n_sample=10, n_domain=5)

    def test_design_effect_must_be_positive(self):
        with pytest.raises(ValueError, match="design_effect"):
            SampleStats(n_sample=10, n_domain=5, population_size=100, design_effect=0.0)


# ----------------------------------------------------------------------
# interval methods
# ----------------------------------------------------------------------


class TestMethodOrdering:
    STATS = SampleStats(
        n_sample=5000,
        n_domain=1200,
        sum_x=30_000.0,
        sum_xx=810_000.0,
        min_x=0.0,
        max_x=90.0,
        population_size=500_000,
    )

    def test_clt_is_tightest_and_hoeffding_loosest(self):
        widths = {
            method: estimate_avg(self.STATS, method=method).half_width
            for method in (
                Method.CLT,
                Method.EMPIRICAL_BERNSTEIN,
                Method.HOEFFDING,
            )
        }
        assert (
            widths[Method.CLT]
            < widths[Method.EMPIRICAL_BERNSTEIN]
            < widths[Method.HOEFFDING]
        )

    def test_finite_sample_methods_need_a_known_range(self):
        no_range = SampleStats(
            n_sample=5000,
            n_domain=1200,
            sum_x=30_000.0,
            sum_xx=810_000.0,
            population_size=500_000,
        )
        strict = estimate_avg(
            no_range, method=Method.EMPIRICAL_BERNSTEIN, fallback_to_clt=False
        )
        assert strict.method is Method.UNDEFINED

    def test_fallback_to_clt_is_recorded_in_notes(self):
        no_range = SampleStats(
            n_sample=5000,
            n_domain=1200,
            sum_x=30_000.0,
            sum_xx=810_000.0,
            population_size=500_000,
        )
        fell_back = estimate_avg(no_range, method=Method.EMPIRICAL_BERNSTEIN)
        assert fell_back.method is Method.CLT
        assert any("fell back" in note for note in fell_back.notes)

    def test_design_effect_widens_the_interval(self):
        deff = SampleStats(
            n_sample=5000,
            n_domain=1200,
            sum_x=30_000.0,
            sum_xx=810_000.0,
            min_x=0.0,
            max_x=90.0,
            population_size=500_000,
            design_effect=25.0,
        )
        assert estimate_avg(deff).half_width == pytest.approx(
            estimate_avg(self.STATS).half_width * 5.0, rel=1e-6
        )


# ----------------------------------------------------------------------
# multiplicity and the query-level contract
# ----------------------------------------------------------------------


class TestMultiplicity:
    def test_bonferroni_splits_the_error_budget(self):
        assert adjust_coverage_level(0.95, 10, Correction.BONFERRONI) == pytest.approx(
            0.995
        )

    def test_sidak_is_slightly_tighter_than_bonferroni(self):
        bonferroni = adjust_coverage_level(0.95, 10, Correction.BONFERRONI)
        sidak = adjust_coverage_level(0.95, 10, Correction.SIDAK)
        assert sidak < bonferroni

    def test_no_correction_leaves_the_level_alone(self):
        assert adjust_coverage_level(0.95, 10, Correction.NONE) == 0.95

    def test_single_interval_needs_no_correction(self):
        assert adjust_coverage_level(0.95, 1, Correction.BONFERRONI) == 0.95


class TestQueryLevelContract:
    @staticmethod
    def _cell(alias, aggregate, group, n_domain=400):
        stats = SampleStats(
            n_sample=4000,
            n_domain=n_domain,
            sum_x=n_domain * 25.0,
            sum_xx=n_domain * (625.0 + 100.0),
            min_x=0.0,
            max_x=80.0,
            population_size=400_000,
        )
        return AggregateCell(
            alias=alias, aggregate=aggregate, stats=stats, group_key=(group,)
        )

    def test_correction_widens_every_interval(self):
        cells = [self._cell("tot", Aggregate.SUM, g) for g in range(8)]
        corrected = estimate_query(cells, correction=Correction.BONFERRONI)
        uncorrected = estimate_query(cells, correction=Correction.NONE)
        assert corrected.per_interval_coverage > uncorrected.per_interval_coverage
        assert corrected.max_relative_half_width > uncorrected.max_relative_half_width

    def test_family_wise_level_only_claimed_when_corrected(self):
        cells = [self._cell("tot", Aggregate.SUM, g) for g in range(4)]
        assert estimate_query(cells, correction=Correction.NONE).family_wise_coverage is None
        assert (
            estimate_query(cells, correction=Correction.BONFERRONI).family_wise_coverage
            == 0.95
        )

    def test_unresolved_cell_blocks_convergence(self):
        """An unobserved group is not an accurate group."""
        cells = [self._cell("avg", Aggregate.AVG, 0)]
        empty = SampleStats(n_sample=4000, n_domain=0, population_size=400_000)
        cells.append(
            AggregateCell(
                alias="avg", aggregate=Aggregate.AVG, stats=empty, group_key=(99,)
            )
        )
        result = estimate_query(cells)
        assert result.unresolved == [((99,), "avg")]
        assert result.meets_target(0.99) is False

    def test_max_relative_half_width_tracks_the_worst_cell(self):
        cells = [
            self._cell("tot", Aggregate.SUM, 0, n_domain=2000),
            self._cell("tot", Aggregate.SUM, 1, n_domain=40),
        ]
        result = estimate_query(cells)
        widths = [e.relative_half_width for e in result.flat()]
        assert result.max_relative_half_width == max(widths)

    def test_empty_input_never_reports_convergence(self):
        result = estimate_query([])
        assert result.meets_target(0.5) is False

    def test_result_is_json_serializable(self):
        import json

        result = estimate_query([self._cell("tot", Aggregate.SUM, 0)])
        json.dumps(result.to_dict())
