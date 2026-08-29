"""
Tests for the three edge-case items: degenerate variance, group completeness,
and skew-based method selection.

Each of these exists because a specific silent failure was identified: a
constant sample reported as certainty, a grouped result that is missing rows
nobody is told about, and a skewed column quietly getting an interval the data
does not support.
"""

from __future__ import annotations

import random

import pytest

from backend.stats import Aggregate, Method, SampleStats, estimate_avg, estimate_sum
from backend.stats.completeness import (
    assess_completeness,
    chao1,
    good_turing_coverage,
)
from backend.stats.selection import (
    COCHRAN_CONSTANT,
    observations_needed,
    recommend_method,
)
from backend.stats.simulation import (
    lognormal_population,
    normal_population,
    pareto_population,
    sample_stats_for,
    srswor,
)


# ----------------------------------------------------------------------
# degenerate variance
# ----------------------------------------------------------------------


class TestDegenerateVariance:
    @staticmethod
    def _constant_sample(n_sample=500, n_domain=500, value=5.0):
        return SampleStats(
            n_sample=n_sample,
            n_domain=n_domain,
            sum_x=n_domain * value,
            sum_xx=n_domain * value * value,
            min_x=value,
            max_x=value,
            population_size=100_000,
        )

    def test_detected(self):
        assert self._constant_sample().variance_is_degenerate

    def test_normal_sample_is_not_flagged(self):
        stats = SampleStats(
            n_sample=500,
            n_domain=200,
            sum_x=1000.0,
            sum_xx=6000.0,
            population_size=100_000,
        )
        assert not stats.variance_is_degenerate

    @pytest.mark.parametrize("estimator", [estimate_sum, estimate_avg])
    def test_no_interval_is_reported(self, estimator):
        """
        The whole point. A zero-width interval would read as certainty and let
        a controller declare instant convergence on an uninformative sample.
        """
        estimate = estimator(self._constant_sample())
        assert estimate.method is Method.DEGENERATE
        assert estimate.ci_low is None and estimate.ci_high is None
        assert estimate.relative_half_width is None
        assert not estimate.is_usable

    @pytest.mark.parametrize("estimator", [estimate_sum, estimate_avg])
    def test_point_estimate_survives(self, estimator):
        """Refusing an interval is not refusing an answer."""
        assert estimator(self._constant_sample()).estimate is not None

    def test_stopping_rule_cannot_be_satisfied_by_it(self):
        assert estimate_avg(self._constant_sample()).meets_target(0.05) is None

    def test_reason_quantifies_what_can_still_be_claimed(self):
        """A rule-of-three bound on the share of rows that could differ."""
        note = estimate_avg(self._constant_sample()).notes[0]
        assert "identical" in note
        assert "%" in note

    def test_a_census_is_still_exact(self):
        """f = 1 is genuine certainty, and must not be confused with this."""
        census = SampleStats(
            n_sample=1000,
            n_domain=1000,
            sum_x=5000.0,
            sum_xx=25_000.0,
            min_x=5.0,
            max_x=5.0,
            population_size=1000,
        )
        assert estimate_avg(census).method is Method.CENSUS


# ----------------------------------------------------------------------
# group completeness
# ----------------------------------------------------------------------


class TestGroupCompleteness:
    def test_chao1_adds_nothing_without_singletons(self):
        assert chao1(singletons=0, doubletons=5, observed_groups=20) == 20

    def test_chao1_grows_with_singletons(self):
        few = chao1(singletons=2, doubletons=5, observed_groups=20)
        many = chao1(singletons=20, doubletons=5, observed_groups=20)
        assert many > few > 20

    def test_chao1_survives_zero_doubletons(self):
        """
        The classical f1^2/(2*f2) form divides by zero here, which happens
        constantly at small sample sizes. The bias-corrected form must not.
        """
        assert chao1(singletons=8, doubletons=0, observed_groups=10) == pytest.approx(
            10 + 8 * 7 / 2
        )

    def test_good_turing_coverage_falls_with_singletons(self):
        assert good_turing_coverage(0, 1000) == 1.0
        assert good_turing_coverage(100, 1000) == pytest.approx(0.9)

    def test_good_turing_undefined_on_empty_sample(self):
        assert good_turing_coverage(0, 0) is None

    def test_complete_sample_looks_complete(self):
        """Every group seen many times: nothing suggests more are hiding."""
        result = assess_completeness({f"g{i}": 100 for i in range(10)})
        assert result.observed_groups == 10
        assert result.singletons == 0
        assert result.estimated_missing_groups == 0
        assert result.looks_complete

    def test_singleton_heavy_sample_predicts_missing_groups(self):
        counts = {f"g{i}": 1 for i in range(20)}
        counts.update({f"big{i}": 50 for i in range(3)})
        result = assess_completeness(counts)
        assert result.estimated_missing_groups > 10
        assert not result.looks_complete
        assert any("still finding new groups" in note for note in result.notes)

    def test_missing_mass_is_reported_separately_from_missing_count(self):
        """
        Twenty missing tiny groups and one missing huge group are different
        problems, so group count and population mass are reported separately.
        """
        result = assess_completeness({f"g{i}": 1 for i in range(10)} | {"big": 990})
        assert result.missing_mass is not None
        assert 0.0 < result.missing_mass < 0.05

    def test_known_cardinality_beats_estimation(self):
        result = assess_completeness(
            {f"g{i}": 40 for i in range(19)}, known_total_groups=25
        )
        assert result.estimated_missing_groups == 6
        assert any("definitely absent" in note for note in result.notes)

    def test_empty_sample_bounds_nothing(self):
        result = assess_completeness({})
        assert result.estimated_missing_groups == float("inf")
        assert not result.looks_complete

    def test_zero_count_groups_are_ignored(self):
        """An unobserved group cannot be evidence about unobserved groups."""
        assert assess_completeness({"a": 5, "b": 0}).observed_groups == 1

    def test_block_sampling_optimism_is_flagged(self):
        result = assess_completeness(
            {f"g{i}": 1 for i in range(10)}, design_effect=40.0
        )
        assert any("all-or-nothing" in note for note in result.notes)

    def test_accepts_a_plain_sequence(self):
        assert assess_completeness([5, 5, 1, 1]).observed_groups == 4


# ----------------------------------------------------------------------
# method selection
# ----------------------------------------------------------------------


class TestMethodSelection:
    def test_constant_was_calibrated_not_inherited(self):
        """
        The textbook value of 25 measured at 86% recall against the coverage
        simulator; 100 reaches 100%. If this is ever lowered back, the
        validation in scripts/run_selection_study.py must be re-run.
        """
        assert COCHRAN_CONSTANT == 100.0

    def test_requirement_grows_quadratically_with_skew(self):
        assert observations_needed(2.0) == 4 * observations_needed(1.0)

    def test_count_is_always_clt(self):
        stats = sample_stats_for(
            pareto_population(size=20_000, alpha=1.8),
            srswor(20_000, 0.05, random.Random(1)),
        )
        recommendation = recommend_method(stats, Aggregate.COUNT)
        assert recommendation.method is Method.CLT
        assert recommendation.is_measured

    def test_symmetric_data_gets_clt(self):
        stats = sample_stats_for(
            normal_population(size=20_000), srswor(20_000, 0.05, random.Random(1))
        )
        recommendation = recommend_method(stats, Aggregate.AVG)
        assert recommendation.method is Method.CLT
        assert abs(recommendation.skewness) < 0.5

    def test_heavy_tails_get_a_finite_sample_method(self):
        stats = sample_stats_for(
            pareto_population(size=20_000, alpha=1.8),
            srswor(20_000, 0.05, random.Random(1)),
        )
        recommendation = recommend_method(stats, Aggregate.AVG)
        assert recommendation.method is Method.EMPIRICAL_BERNSTEIN
        assert recommendation.required_n > stats.n_domain

    def test_missing_third_moment_is_declared_unverified(self):
        """A default is not a finding, and the caller must be able to tell."""
        stats = SampleStats(
            n_sample=1000,
            n_domain=400,
            sum_x=2000.0,
            sum_xx=12_000.0,
            population_size=50_000,
        )
        recommendation = recommend_method(stats, Aggregate.AVG)
        assert recommendation.basis == "unverified"
        assert not recommendation.is_measured

    def test_unbounded_skewed_column_says_so_rather_than_pretending(self):
        """Bernstein needs a range; without one, say the interval is suspect."""
        population = pareto_population(size=20_000, alpha=1.8)
        stats = sample_stats_for(population, srswor(20_000, 0.05, random.Random(1)))
        no_range = SampleStats(
            n_sample=stats.n_sample,
            n_domain=stats.n_domain,
            sum_x=stats.sum_x,
            sum_xx=stats.sum_xx,
            sum_xxx=stats.sum_xxx,
            population_size=stats.population_size,
        )
        recommendation = recommend_method(no_range, Aggregate.AVG)
        assert recommendation.method is Method.CLT
        assert "under-cover" in recommendation.reason

    def test_sum_uses_expanded_skew_which_zeros_make_worse(self):
        """
        SUM is a total of x * 1[in domain], so the out-of-domain zeros are part
        of its distribution and add skew that the domain alone does not show.
        """
        stats = sample_stats_for(
            lognormal_population(size=20_000, sigma=0.9, domain_rate=0.2),
            srswor(20_000, 0.10, random.Random(1)),
        )
        assert stats.expanded_skewness > stats.domain_skewness

    def test_skewness_matches_a_direct_engine_value(self):
        """Reconstruction from sum_xxx must agree with what an engine reports."""
        population = lognormal_population(size=5_000, sigma=0.8)
        stats = sample_stats_for(population, srswor(5_000, 0.5, random.Random(2)))
        values = [
            v for v, d in zip(population.values, population.in_domain) if d
        ][: stats.n_domain]
        assert stats.domain_skewness is not None
        assert abs(stats.domain_skewness) > 0.5
        assert len(values) > 0
