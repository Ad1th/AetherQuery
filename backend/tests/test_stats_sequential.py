"""
Tests for the anytime-valid layer.

Two groups. The first checks the alpha-spending arithmetic and the controller
contract. The second is experimental and records the measured result: that
AetherQuery's current stopping rules do *not* induce optional-stopping bias,
while a rule that thresholds the interval's own coverage statistic destroys it
completely and is repaired by this module.

That pairing matters. A correction that is never exercised is indistinguishable
from a correction that does not work, so the positive control is not optional
decoration -- it is what makes the negative result meaningful.
"""

from __future__ import annotations

import random

import pytest

from backend.stats.api import AggregateCell, estimate_query
from backend.stats.contracts import Aggregate, Correction, Method, SampleStats
from backend.stats.sequential import (
    Schedule,
    SequentialAnalysis,
    alpha_for_look,
    price_of_peeking,
)
from backend.stats.simulation import (
    normal_population,
    sample_stats_for,
    srswor,
    wilson_interval,
)

PROGRESSION = [0.005 * 1.15 ** i for i in range(20)]
TRIALS = 500


def _cells(stats, aggregate=Aggregate.AVG):
    return [AggregateCell(alias=aggregate.value, aggregate=aggregate, stats=stats)]


# ----------------------------------------------------------------------
# alpha spending
# ----------------------------------------------------------------------


class TestAlphaSpending:
    def test_uniform_splits_evenly(self):
        for look in range(1, 6):
            assert alpha_for_look(Schedule.UNIFORM, 0.05, look, 5) == pytest.approx(
                0.01
            )

    def test_uniform_sums_to_the_budget(self):
        total = sum(
            alpha_for_look(Schedule.UNIFORM, 0.05, look, 5) for look in range(1, 6)
        )
        assert total == pytest.approx(0.05)

    def test_harmonic_sums_to_the_budget_in_the_limit(self):
        """alpha / (t(t+1)) telescopes to exactly alpha."""
        total = sum(
            alpha_for_look(Schedule.HARMONIC, 0.05, look) for look in range(1, 20_001)
        )
        assert total == pytest.approx(0.05, rel=1e-3)
        assert total < 0.05

    def test_harmonic_never_exhausts_the_budget(self):
        """Unlimited peeking stays covered, at a level that keeps tightening."""
        for looks in (10, 100, 1000):
            total = sum(
                alpha_for_look(Schedule.HARMONIC, 0.05, look)
                for look in range(1, looks + 1)
            )
            assert total < 0.05

    def test_uniform_needs_max_looks(self):
        with pytest.raises(ValueError, match="max_looks"):
            alpha_for_look(Schedule.UNIFORM, 0.05, 1)

    def test_overrunning_a_uniform_budget_falls_back_not_silently_reuses(self):
        """
        Past the declared number of looks there is nothing left to spend.
        Reusing the planned level would break the union bound silently.
        """
        planned = alpha_for_look(Schedule.UNIFORM, 0.05, 5, 5)
        overrun = alpha_for_look(Schedule.UNIFORM, 0.05, 6, 5)
        assert overrun < planned

    def test_looks_are_one_based(self):
        with pytest.raises(ValueError, match="1-based"):
            alpha_for_look(Schedule.HARMONIC, 0.05, 0)

    def test_price_of_peeking_is_monotone_for_harmonic(self):
        rows = price_of_peeking(0.95, 6, Schedule.HARMONIC)
        multipliers = [multiplier for _, _, multiplier in rows]
        assert multipliers == sorted(multipliers)
        assert multipliers[0] > 1.0

    def test_uniform_is_tighter_than_harmonic_at_the_last_look(self):
        """Knowing the number of looks in advance is worth something."""
        uniform = price_of_peeking(0.95, 5, Schedule.UNIFORM)[-1][2]
        harmonic = price_of_peeking(0.95, 5, Schedule.HARMONIC)[-1][2]
        assert uniform < harmonic


# ----------------------------------------------------------------------
# the controller contract
# ----------------------------------------------------------------------


class TestSequentialAnalysis:
    @staticmethod
    def _stats(n_sample=4000, n_domain=1000):
        return SampleStats(
            n_sample=n_sample,
            n_domain=n_domain,
            sum_x=n_domain * 50.0,
            sum_xx=n_domain * (2500.0 + 144.0),
            min_x=0.0,
            max_x=120.0,
            population_size=400_000,
        )

    def test_defaults_to_uniform_when_looks_are_known(self):
        assert SequentialAnalysis(max_looks=5).schedule is Schedule.UNIFORM

    def test_defaults_to_harmonic_when_they_are_not(self):
        assert SequentialAnalysis().schedule is Schedule.HARMONIC

    def test_budget_is_spent_monotonically(self):
        analysis = SequentialAnalysis(0.95, max_looks=4)
        spent = []
        for _ in range(4):
            spent.append(analysis.look(_cells(self._stats())).alpha_spent)
        assert spent == sorted(spent)
        assert spent[-1] == pytest.approx(0.05)

    def test_budget_is_never_overspent_within_the_plan(self):
        analysis = SequentialAnalysis(0.95, max_looks=6)
        for _ in range(6):
            result = analysis.look(_cells(self._stats()))
        assert result.alpha_spent <= 0.05 + 1e-9
        assert result.alpha_remaining >= 0.0

    def test_intervals_widen_with_each_look(self):
        """Later looks cost more, and the intervals must show it."""
        analysis = SequentialAnalysis(0.95, max_looks=6, schedule=Schedule.HARMONIC)
        widths = []
        for _ in range(4):
            result = analysis.look(_cells(self._stats()))
            widths.append(
                result.estimates.estimates[None]["avg"].half_width
            )
        assert widths == sorted(widths)

    def test_sequential_interval_is_wider_than_a_naive_one(self):
        stats = self._stats()
        naive = estimate_query(
            _cells(stats), 0.95, Method.CLT, Correction.NONE
        ).estimates[None]["avg"]
        analysis = SequentialAnalysis(0.95, max_looks=5, correction=Correction.NONE)
        sequential = analysis.look(_cells(stats)).estimates.estimates[None]["avg"]
        assert sequential.half_width > naive.half_width

    def test_overrun_is_flagged_in_the_notes(self):
        analysis = SequentialAnalysis(0.95, max_looks=2)
        for _ in range(3):
            result = analysis.look(_cells(self._stats()))
        assert any("only 2 were declared" in note for note in result.notes)

    def test_history_and_summary_track_every_look(self):
        analysis = SequentialAnalysis(0.95, max_looks=3)
        for _ in range(3):
            analysis.look(_cells(self._stats()))
        summary = analysis.summary()
        assert summary["looks"] == 3
        assert len(analysis.history) == 3
        assert len(summary["per_look_coverage"]) == 3

    def test_multiplicity_is_applied_per_look_so_growing_groups_are_covered(self):
        """
        Each look pays only for the cells it reports, so a group set that grows
        as sampling proceeds stays covered -- the loose end in the single-look
        API.
        """
        analysis = SequentialAnalysis(0.95, max_looks=3)
        first = analysis.look(_cells(self._stats()))
        wider = analysis.look(
            [
                AggregateCell(
                    alias=f"g{i}",
                    aggregate=Aggregate.AVG,
                    stats=self._stats(),
                    group_key=(i,),
                )
                for i in range(8)
            ]
        )
        assert first.estimates.num_intervals == 1
        assert wider.estimates.num_intervals == 8
        assert wider.estimates.per_interval_coverage > wider.per_look_coverage

    def test_should_stop_requires_every_cell_resolved(self):
        empty = SampleStats(n_sample=4000, n_domain=0, population_size=400_000)
        analysis = SequentialAnalysis(0.95, max_looks=3)
        result = analysis.look(
            [
                AggregateCell("avg", Aggregate.AVG, self._stats(), group_key=(1,)),
                AggregateCell("avg", Aggregate.AVG, empty, group_key=(2,)),
            ]
        )
        assert result.should_stop(0.99) is False

    def test_result_is_json_serializable(self):
        import json

        analysis = SequentialAnalysis(0.95, max_looks=3)
        json.dumps(analysis.look(_cells(self._stats())).to_dict())


# ----------------------------------------------------------------------
# measured behaviour under real stopping rules
# ----------------------------------------------------------------------


def _walk(population, rule, corrected, rng, truth):
    """Run one progression under a stopping rule; return the interval shown."""
    analysis = (
        SequentialAnalysis(0.95, max_looks=len(PROGRESSION), correction=Correction.NONE)
        if corrected
        else None
    )
    previous = last = None
    for look, fraction in enumerate(PROGRESSION, start=1):
        stats = sample_stats_for(population, srswor(len(population), fraction, rng))
        cells = _cells(stats)
        if corrected:
            estimate = analysis.look(cells).estimates.estimates[None]["avg"]
        else:
            estimate = estimate_query(
                cells, 0.95, Method.CLT, Correction.NONE
            ).estimates[None]["avg"]
        if not estimate.is_usable:
            continue
        last = estimate

        if rule == "convergence":
            if previous not in (None, 0) and look >= 2:
                if abs(estimate.estimate - previous) / abs(previous) < 0.04:
                    return estimate
        elif rule == "foregone":
            if not (estimate.ci_low <= truth <= estimate.ci_high):
                return estimate
        previous = estimate.estimate
    return last


def _coverage(rule, corrected, trials=TRIALS, seed=5):
    population = normal_population(size=20_000)
    truth = population.truth(Aggregate.AVG)
    rng = random.Random(seed)
    covered = resolved = 0
    for _ in range(trials):
        estimate = _walk(population, rule, corrected, rng, truth)
        if estimate is None or not estimate.is_usable:
            continue
        resolved += 1
        if estimate.ci_low <= truth <= estimate.ci_high:
            covered += 1
    return covered / resolved, wilson_interval(covered, resolved)


class TestMeasuredStoppingBehaviour:
    """
    Recorded experimental results. A failure here means the finding changed,
    which is a reason to re-examine the claim rather than relax the test.
    """

    def test_convergence_rule_does_not_break_naive_intervals(self):
        """
        The negative result. AetherQuery's current rule thresholds a difference
        between estimates, not the interval's coverage statistic, so it does
        not induce optional-stopping bias.
        """
        coverage, (_, high) = _coverage("convergence", corrected=False)
        assert high >= 0.95, (
            f"convergence rule covered {coverage:.2%}; the finding that "
            "AetherQuery's stopping rule is safe has changed"
        )

    def test_foregone_conclusion_destroys_naive_intervals(self):
        """
        The positive control. Thresholding the coverage statistic is the thing
        that actually breaks fixed-sample intervals.
        """
        coverage, _ = _coverage("foregone", corrected=False)
        assert coverage < 0.60, (
            f"expected the positive control to collapse, got {coverage:.2%}; "
            "if this passes, the harness is not measuring optional stopping"
        )

    def test_alpha_spending_repairs_the_foregone_conclusion(self):
        """And the correction has to rescue it, or it does not work."""
        coverage, (_, high) = _coverage("foregone", corrected=True)
        assert high >= 0.95, f"sequential intervals covered only {coverage:.2%}"

    def test_the_repair_is_large(self):
        naive, _ = _coverage("foregone", corrected=False)
        repaired, _ = _coverage("foregone", corrected=True)
        assert repaired > naive + 0.50
