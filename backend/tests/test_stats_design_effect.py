"""
Tests for the design-effect module and the Thesis-B result.

Two kinds of test here. The first kind checks the cluster estimator's algebra
and its handling of the cases where it must decline. The second kind locks in
the measured experimental result: that block sampling on physically clustered
data destroys interval validity, and that treating the block as the sampling
unit restores it.

The experimental tests are deliberately small -- enough blocks and trials to
make the effect unmistakable, not enough to resolve it precisely. The full
study is ``scripts/run_design_effect_study.py``.
"""

from __future__ import annotations

import random
import statistics

import pytest

from backend.stats.contracts import Aggregate, Method
from backend.stats.design_effect import (
    DUCKDB_VECTOR_SIZE,
    BlockAggregate,
    ClusterSampleStats,
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
    wilson_interval,
)

SIZE = 60_000
BLOCK = 64
FRACTION = 0.05
TRIALS = 400


@pytest.fixture(scope="module")
def layouts():
    """One population in two physical orders, filtered on the ordering column."""
    base = normal_population(size=SIZE)
    built = {}
    for mode in ("shuffled", "clustered"):
        ordered = reorder(base, mode)
        threshold = statistics.median(ordered.values)
        built[mode] = Population(
            name=mode,
            values=list(ordered.values),
            in_domain=[value > threshold for value in ordered.values],
        )
    return built


def measure(population, aggregate, estimator, trials=TRIALS, seed=7):
    truth = population.truth(aggregate)
    sampler = block_sampler(BLOCK)
    rng = random.Random(seed)
    covered = resolved = 0
    for _ in range(trials):
        indices = sampler(len(population), FRACTION, rng)
        if estimator == "srs":
            estimate = estimate_aggregate(
                aggregate, sample_stats_for(population, indices), method=Method.CLT
            )
        else:
            estimate = estimate_clustered(
                aggregate, cluster_stats_for(population, indices, block_size=BLOCK)
            )
        if not estimate.is_usable:
            continue
        resolved += 1
        if estimate.ci_low <= truth <= estimate.ci_high:
            covered += 1
    return covered / resolved if resolved else None


# ----------------------------------------------------------------------
# the cluster estimator
# ----------------------------------------------------------------------


class TestClusterSampleStats:
    @staticmethod
    def _cluster(n_blocks=10, per_block=100, domain=50, value=2.0):
        blocks = tuple(
            BlockAggregate(
                block_id=i,
                n_rows=per_block,
                n_domain=domain,
                sum_x=domain * value,
                sum_xx=domain * value * value,
            )
            for i in range(n_blocks)
        )
        return ClusterSampleStats(
            blocks=blocks,
            total_blocks=100,
            population_size=100 * per_block,
        )

    def test_aggregates_across_blocks(self):
        cluster = self._cluster()
        assert cluster.blocks_sampled == 10
        assert cluster.n_sample == 1000
        assert cluster.n_domain == 500
        assert cluster.block_fraction == pytest.approx(0.10)
        assert cluster.mean_block_size == pytest.approx(100.0)

    def test_avg_block_totals_are_residuals_summing_to_zero(self):
        """The signature of a correctly linearized ratio estimator."""
        blocks = (
            BlockAggregate(0, 100, 40, 80.0, 200.0),
            BlockAggregate(1, 100, 60, 300.0, 1600.0),
            BlockAggregate(2, 100, 50, 150.0, 500.0),
        )
        cluster = ClusterSampleStats(blocks, total_blocks=50, population_size=5000)
        residuals = cluster.block_totals(Aggregate.AVG)
        assert sum(residuals) == pytest.approx(0.0, abs=1e-9)

    def test_point_estimates_match_the_srs_view(self):
        """The design changes the variance, never the point estimate."""
        cluster = self._cluster()
        for aggregate in (Aggregate.COUNT, Aggregate.SUM, Aggregate.AVG):
            clustered = estimate_clustered(aggregate, cluster)
            srs = estimate_aggregate(aggregate, cluster.as_srs())
            assert clustered.estimate == pytest.approx(srs.estimate, rel=1e-9)

    def test_declines_with_fewer_than_two_blocks(self):
        """A between-block variance needs at least two blocks, whatever n is."""
        cluster = self._cluster(n_blocks=1)
        estimate = estimate_clustered(Aggregate.SUM, cluster)
        assert estimate.estimate is not None
        assert estimate.ci_low is None
        assert any("at least 2" in note for note in estimate.notes)

    def test_degrees_of_freedom_come_from_blocks_not_rows(self):
        """
        Ten blocks of a hundred rows give 9 df, not 999. This is the entire
        reason cluster intervals are wider.
        """
        cluster = self._cluster(n_blocks=10, per_block=100)
        estimate = estimate_clustered(Aggregate.SUM, cluster)
        assert any("degrees of freedom" in note for note in estimate.notes)
        assert estimate.n_sample == 1000

    def test_small_block_count_is_flagged(self):
        estimate = estimate_clustered(Aggregate.SUM, self._cluster(n_blocks=5))
        assert any("indicative" in note for note in estimate.notes)

    def test_identical_blocks_give_zero_between_variance(self):
        """
        Perfectly uniform blocks carry no between-block information, so the
        estimator reports zero variance -- correct, and a reminder that a
        zero-width cluster interval means uninformative, not certain.
        """
        estimate = estimate_clustered(Aggregate.SUM, self._cluster())
        assert estimate.variance == pytest.approx(0.0)


class TestDesignEffectArithmetic:
    def test_kish_identity_round_trips(self):
        rho = intraclass_correlation(1.0 + 99 * 0.05, 100.0)
        assert rho == pytest.approx(0.05)

    def test_no_correlation_means_no_design_effect(self):
        assert intraclass_correlation(1.0, 2048.0) == pytest.approx(0.0)

    def test_projection_scales_with_block_size(self):
        """Bigger blocks make clustering worse, never better."""
        small = project_design_effect(11.0, 100.0, 100.0)
        large = project_design_effect(11.0, 100.0, 2048.0)
        assert small == pytest.approx(11.0)
        assert large > small

    def test_projection_declines_on_sub_unit_design_effects(self):
        """
        A DEFF below 1 is noise around 'no clustering'. Projecting it would
        claim an effective sample larger than the sample itself.
        """
        assert project_design_effect(0.9, 256.0) is None
        assert project_design_effect(1.0, 256.0) is None

    def test_duckdb_block_size_is_the_measured_one(self):
        """Probed directly: SYSTEM samples 2048-row vectors, not row groups."""
        assert DUCKDB_VECTOR_SIZE == 2048

    def test_avg_needs_more_blocks_than_totals(self):
        assert minimum_blocks_for_valid_interval(
            Aggregate.AVG
        ) > minimum_blocks_for_valid_interval(Aggregate.SUM)


# ----------------------------------------------------------------------
# Thesis B, as a regression lock
# ----------------------------------------------------------------------


class TestThesisB:
    """
    The measured experimental result. If any of these change, the paper's
    central empirical claim has changed and needs re-examining -- they are not
    tests to relax.
    """

    @pytest.mark.parametrize(
        "aggregate", [Aggregate.COUNT, Aggregate.SUM, Aggregate.AVG]
    )
    def test_shuffled_layout_is_unharmed_by_block_sampling(self, layouts, aggregate):
        """
        Block sampling is not harmful in itself. With representative blocks the
        SRS interval is still fine, which is what makes physical clustering
        rather than the sampler the cause.
        """
        coverage = measure(layouts["shuffled"], aggregate, "srs")
        assert coverage is not None
        assert wilson_interval(
            round(coverage * TRIALS), TRIALS
        )[1] >= 0.95, f"shuffled coverage {coverage:.2%}"

    @pytest.mark.parametrize(
        "aggregate", [Aggregate.COUNT, Aggregate.SUM, Aggregate.AVG]
    )
    def test_clustered_layout_destroys_srs_interval_validity(
        self, layouts, aggregate
    ):
        """
        The headline finding. Same rows, same truth, same sampler -- only the
        physical order differs, and nominal 95% intervals collapse.
        """
        coverage = measure(layouts["clustered"], aggregate, "srs")
        assert coverage is not None
        assert coverage < 0.60, (
            f"{aggregate.value} covered {coverage:.2%} on clustered data; "
            "the collapse this test locks in has changed"
        )

    @pytest.mark.parametrize("aggregate", [Aggregate.COUNT, Aggregate.SUM])
    def test_cluster_estimator_repairs_totals(self, layouts, aggregate):
        coverage = measure(layouts["clustered"], aggregate, "cluster")
        assert coverage is not None
        assert wilson_interval(round(coverage * TRIALS), TRIALS)[1] >= 0.95, (
            f"{aggregate.value} cluster coverage {coverage:.2%}"
        )

    def test_cluster_estimator_substantially_repairs_avg(self, layouts):
        """
        AVG is a ratio, and its cluster interval converges to nominal more
        slowly than the totals do: measured coverage ran 84.0% at 10 sampled
        blocks, 91.2% at 39, 94.7% at 312 and 94.8% at 625. So the repair is
        large but, at modest block counts, incomplete. Asserting the repair
        rather than full nominal coverage records that honestly.
        """
        broken = measure(layouts["clustered"], Aggregate.AVG, "srs")
        repaired = measure(layouts["clustered"], Aggregate.AVG, "cluster")
        assert broken < 0.60
        assert repaired > 0.85
        assert repaired > broken + 0.30

    def test_measured_design_effect_is_large_on_clustered_data(self, layouts):
        sampler = block_sampler(BLOCK)
        rng = random.Random(11)
        indices = sampler(SIZE, FRACTION, rng)

        clustered = cluster_stats_for(
            layouts["clustered"], indices, block_size=BLOCK
        )
        shuffled = cluster_stats_for(
            layouts["shuffled"], indices, block_size=BLOCK
        )

        clustered_deff = estimate_design_effect(Aggregate.SUM, clustered)
        shuffled_deff = estimate_design_effect(Aggregate.SUM, shuffled)

        assert clustered_deff is not None and shuffled_deff is not None
        assert shuffled_deff < 3.0, "random layout should show little clustering"
        assert clustered_deff > 20.0, "sorted layout should show a large one"

    def test_design_effect_is_undefined_below_two_blocks(self):
        cluster = ClusterSampleStats(
            blocks=(BlockAggregate(0, 100, 50, 100.0, 250.0),),
            total_blocks=100,
            population_size=10_000,
        )
        assert estimate_design_effect(Aggregate.SUM, cluster) is None
