"""
Anytime-valid intervals: staying honest while peeking.

The problem
-----------
Every interval produced so far is valid for **one** look at the data. The
sampling controller does not take one look. It samples, inspects the result,
samples more, inspects again, and stops the moment the answer looks settled.
That is *optional stopping*, and it invalidates fixed-sample intervals: the
stopping time is chosen using the same data the interval is built from, so the
controller preferentially halts at exactly the moments when random fluctuation
made the answer look better-determined than it is. The reported coverage is
then an overstatement, and how large an overstatement depends on the rule.

The current rule is the worst case for this. ``runtime_sampling`` stops when
two successive estimates agree to within a threshold, which selects for
*stability*, not accuracy. Two consecutive samples can agree closely and both
be wrong, and a biased estimator agrees with itself perfectly.

What fixes it
-------------
A confidence sequence: a family of intervals valid *simultaneously across all
looks*, so that

    P( for every look t: theta lies in [L_t, U_t] ) >= 1 - alpha

With the quantifier inside the probability, any stopping rule at all -- data
dependent, adversarial, whatever -- lands on an interval that still holds.

Why alpha-spending here, and not a betting confidence sequence
--------------------------------------------------------------
The tight modern constructions (Howard et al.'s time-uniform Chernoff bounds,
Waudby-Smith & Ramdas's betting sequences) assume a *growing stream*: look t+1
sees everything look t saw, plus more. That is a martingale, and the tightness
comes from exploiting it.

AetherQuery does not work that way. ``fetch_aggregated_sample`` issues a fresh
``TABLESAMPLE`` on every iteration, so look t+1 is an independent re-draw that
does not contain look t's rows. There is no martingale to exploit, and applying
a stream construction here would be a real error rather than a conservative
approximation.

For independent looks the correct and simple tool is a union bound with an
alpha-spending schedule: allocate ``alpha_t`` to look t with the total no more
than alpha. It composes with every method in this package -- CLT, empirical
Bernstein, Hoeffding, the cluster estimator -- because it only changes the
confidence level each is asked for.

Worth flagging to whoever owns the controller: making the iterations *nested*
(accumulating rows rather than re-drawing) would both stop discarding all the
work done in earlier iterations and unlock the tighter martingale bounds. That
is a controller design decision, not a statistical one, so it is raised here
rather than assumed.

What the measurements actually showed
-------------------------------------
Optional stopping is a real phenomenon, but it did **not** damage AetherQuery's
current rules. Measured over 1500 trials per configuration
(``scripts/run_sequential_study.py``), coverage at the stopping time:

    rule          naive fixed 95%     sequential
    oracle          94.5 - 95.5%      98.5 - 99.2%
    precision       94.1 - 95.7%      98.5 - 99.5%
    convergence     96.5 - 97.3%      99.3 - 99.7%

Nothing under-covered. Two follow-up hypotheses for why were tested and both
were wrong: nesting the samples instead of re-drawing did not break it
(95.8%), and peeking 25 times instead of 5 did not break it (94.8%).

The mechanism is narrower than "peeking is dangerous". Fixed-sample intervals
break under optional stopping when the stopping rule **thresholds the
interval's own coverage statistic** -- when it asks, in effect, whether the
estimate is far enough from a reference relative to its own standard error.
That is the classic "sampling to a foregone conclusion": keep looking and you
will eventually cross by chance.

Neither AetherQuery rule does that. The precision rule thresholds
``half_width / |estimate|``, which is mostly a function of sample size. The
convergence rule thresholds ``|est_t - est_{t-1}|``, a difference between
estimates rather than a standardised deviation from a null. Neither is the
coverage statistic, so neither induces the bias.

That this module is nevertheless correct, and not merely unexercised, was
checked against a rule that *does* threshold the coverage statistic -- stop as
soon as the interval excludes a reference value. Naive coverage collapsed to
**25.0%**; the same run through ``SequentialAnalysis`` held at **94.1%**.

Spending does not repair a miscalibrated per-look interval
----------------------------------------------------------
The two corrections are orthogonal, and the positive control shows it. Under
the foregone-conclusion rule, alpha-spending restored coverage fully on a
normal population (95.5%, 94.7%) but left a residual shortfall on a skewed one
(93.7%, 93.7%).

That is not a failure of the schedule. A union bound is only ever as good as
the intervals it is bounding: if skew makes each nominal-99% look deliver 98%,
five of them give at best ``1 - 5*0.02 = 90%``, not 95%. Switching the per-look
method to empirical Bernstein -- which is calibrated on skewed data, where the
CLT is not -- restored coverage to 100% for both aggregates.

So sequential validity composes *on top of* per-look validity and cannot
substitute for it. On skewed columns the honest configuration is a
finite-sample per-look method inside the spending schedule, and the widths
multiply.

When this becomes necessary
---------------------------
Use it for any stopping rule that compares an interval against a reference
value or a hypothesis -- "stop once we are confident revenue exceeds X",
"stop once two groups are distinguishable", early termination of a search.
Those are the foregone-conclusion pattern and they will silently destroy
coverage without it.

For the current width-based and convergence-based rules it is insurance rather
than a repair, and the cost is real: at alpha = 0.05 over five looks the
uniform schedule computes each interval at 99%, widening it about 1.3x. Whether
to pay that for a bias none of the present rules exhibits is a judgement call
for whoever owns the controller, and it should be made knowing the measurement
above rather than on the general reputation of optional stopping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from backend.stats.api import AggregateCell, EstimateSet, estimate_query
from backend.stats.contracts import Correction, Method
from backend.stats.estimators import DEFAULT_COVERAGE_LEVEL


class Schedule(str, Enum):
    """How the error budget is spread across looks."""

    UNIFORM = "uniform"
    """``alpha / T``. Needs the number of looks in advance, and is the tighter
    choice when it is known -- which it usually is, since a sampling
    progression has a fixed length."""

    HARMONIC = "harmonic"
    """``alpha / (t * (t+1))``, which sums to exactly alpha over infinitely
    many looks. Allows unlimited peeking at the price of a level that keeps
    tightening. The right default when the controller may insert extra
    iterations adaptively, as this one does."""


def alpha_for_look(
    schedule: Schedule,
    alpha: float,
    look: int,
    max_looks: int | None = None,
) -> float:
    """
    Error budget for a single look.

    ``look`` is 1-based. Both schedules satisfy ``sum of alpha_t <= alpha``,
    which is what the union bound needs.
    """
    if look < 1:
        raise ValueError("look is 1-based")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must lie strictly in (0, 1)")

    if schedule is Schedule.UNIFORM:
        if max_looks is None:
            raise ValueError("the uniform schedule needs max_looks")
        if look > max_looks:
            # Past the declared budget there is nothing left to spend. Rather
            # than silently reusing a level that is no longer covered by the
            # union bound, fall back to the harmonic tail.
            return alpha / (look * (look + 1))
        return alpha / max_looks

    if schedule is Schedule.HARMONIC:
        return alpha / (look * (look + 1))

    raise ValueError(f"unknown schedule: {schedule}")


@dataclass(frozen=True)
class SequentialResult:
    """
    One look's worth of anytime-valid intervals, plus budget accounting.

    ``estimates`` holds intervals that remain valid however this look was
    arrived at and whatever the controller decides next. ``family_wise_coverage``
    on that EstimateSet is the level *within* this look; the sequential
    guarantee across looks is ``sequential_coverage_level`` here.
    """

    look: int
    estimates: EstimateSet
    alpha_this_look: float
    alpha_spent: float
    alpha_budget: float
    sequential_coverage_level: float
    schedule: Schedule
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def alpha_remaining(self) -> float:
        return max(0.0, self.alpha_budget - self.alpha_spent)

    @property
    def per_look_coverage(self) -> float:
        """The level this look's intervals were computed at."""
        return 1.0 - self.alpha_this_look

    def should_stop(self, target_relative_error: float) -> bool:
        """
        May the controller stop here?

        True only when every cell has an interval and every one is inside the
        target. Because these intervals are anytime-valid, acting on this
        answer does not invalidate them -- which is the entire point of the
        module.
        """
        return self.estimates.meets_target(target_relative_error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "look": self.look,
            "alpha_this_look": self.alpha_this_look,
            "alpha_spent": self.alpha_spent,
            "alpha_budget": self.alpha_budget,
            "alpha_remaining": self.alpha_remaining,
            "per_look_coverage": self.per_look_coverage,
            "sequential_coverage_level": self.sequential_coverage_level,
            "schedule": self.schedule.value,
            "estimates": self.estimates.to_dict(),
            "notes": list(self.notes),
        }


class SequentialAnalysis:
    """
    The contract the sampling controller consumes.

    Call ``look()`` once per sampling iteration and consult ``should_stop`` on
    the result. Stop whenever it says so, or whenever the controller's own
    budget runs out; the intervals hold either way::

        analysis = SequentialAnalysis(coverage_level=0.95, max_looks=len(progression))

        for fraction in progression:
            cells = build_cells(run_sampled_query(fraction))
            result = analysis.look(cells)
            if result.should_stop(target_relative_error=0.05):
                break

        # result.estimates are valid despite having peeked at every iteration

    Two corrections compose here, and both are necessary. Across looks, the
    schedule splits alpha over time. Within a look, the multiplicity correction
    splits that look's share across however many cells the result grid has.
    Because the second split happens per look, a group set that *grows* as
    sampling proceeds is handled automatically -- each look pays only for the
    cells it actually reports, which was the loose end left in the
    single-look API.
    """

    def __init__(
        self,
        coverage_level: float = DEFAULT_COVERAGE_LEVEL,
        schedule: Schedule | None = None,
        max_looks: int | None = None,
        correction: Correction = Correction.BONFERRONI,
        method: Method = Method.CLT,
    ) -> None:
        if not 0.0 < coverage_level < 1.0:
            raise ValueError("coverage_level must lie strictly in (0, 1)")

        if schedule is None:
            # A known number of looks makes the uniform split strictly tighter,
            # so prefer it whenever the caller can say how many there will be.
            schedule = Schedule.UNIFORM if max_looks else Schedule.HARMONIC

        if schedule is Schedule.UNIFORM and max_looks is None:
            raise ValueError("the uniform schedule needs max_looks")

        self.coverage_level = coverage_level
        self.alpha_budget = 1.0 - coverage_level
        self.schedule = schedule
        self.max_looks = max_looks
        self.correction = correction
        self.method = method

        self._look = 0
        self._alpha_spent = 0.0
        self._history: list[SequentialResult] = []

    @property
    def looks_taken(self) -> int:
        return self._look

    @property
    def alpha_spent(self) -> float:
        return self._alpha_spent

    @property
    def history(self) -> list[SequentialResult]:
        return list(self._history)

    def look(
        self,
        cells: Sequence[AggregateCell],
        method: Method | None = None,
    ) -> SequentialResult:
        """
        Take one look at a fresh sample and get anytime-valid intervals back.

        Every call spends budget, including calls whose result is discarded, so
        a caller that computes intervals it does not use still pays for them.
        That is not an accounting quirk -- looking is what costs, whether or not
        the look is acted on.
        """
        self._look += 1
        alpha_this_look = alpha_for_look(
            self.schedule, self.alpha_budget, self._look, self.max_looks
        )
        self._alpha_spent += alpha_this_look

        estimates = estimate_query(
            cells,
            coverage_level=1.0 - alpha_this_look,
            method=method or self.method,
            correction=self.correction,
        )

        notes: tuple[str, ...] = (
            f"look {self._look} of a sequential analysis; interval computed at "
            f"{(1.0 - alpha_this_look):.4%} so that all looks together hold at "
            f"{self.coverage_level:.2%}",
        )

        if self.max_looks is not None and self._look > self.max_looks:
            notes = notes + (
                f"this is look {self._look} but only {self.max_looks} were "
                "declared; the schedule has fallen back to a harmonic tail, "
                "which keeps the guarantee but is looser than planning for the "
                "true number of looks would have been",
            )

        if self._alpha_spent > self.alpha_budget * 1.000001:
            notes = notes + (
                "the declared error budget is exhausted; further looks are no "
                "longer covered by the sequential guarantee",
            )

        result = SequentialResult(
            look=self._look,
            estimates=estimates,
            alpha_this_look=alpha_this_look,
            alpha_spent=self._alpha_spent,
            alpha_budget=self.alpha_budget,
            sequential_coverage_level=self.coverage_level,
            schedule=self.schedule,
            notes=notes,
        )
        self._history.append(result)
        return result

    def summary(self) -> dict[str, Any]:
        """Budget accounting across the whole run, for logs and the paper."""
        return {
            "looks": self._look,
            "schedule": self.schedule.value,
            "max_looks": self.max_looks,
            "coverage_level": self.coverage_level,
            "alpha_budget": self.alpha_budget,
            "alpha_spent": self._alpha_spent,
            "alpha_remaining": max(0.0, self.alpha_budget - self._alpha_spent),
            "per_look_coverage": [
                result.per_look_coverage for result in self._history
            ],
        }


def price_of_peeking(
    coverage_level: float,
    looks: int,
    schedule: Schedule = Schedule.UNIFORM,
) -> list[tuple[int, float, float]]:
    """
    What each look costs, as a table of ``(look, level, width multiplier)``.

    The multiplier is relative to a naive fixed 95%-style interval at the same
    variance, so it isolates what sequential validity costs from everything
    else. Useful for the paper, and for anyone deciding how many iterations a
    progression should have: more looks are not free.
    """
    from backend.stats.intervals import normal_quantile

    alpha = 1.0 - coverage_level
    baseline = normal_quantile(coverage_level)

    rows: list[tuple[int, float, float]] = []
    for look in range(1, looks + 1):
        alpha_t = alpha_for_look(schedule, alpha, look, looks)
        level = 1.0 - alpha_t
        rows.append((look, level, normal_quantile(level) / baseline))
    return rows
