"""
Query-level statistical interface.

The estimators in estimators.py handle one aggregate over one domain. A real
AetherQuery result is a grid: every GROUP BY group crossed with every aggregate
in the SELECT list. This module estimates that whole grid at once and, crucially,
handles the multiplicity problem that arises from doing so.

The controller consumes exactly two things from here:

    estimate_query(cells, ...) -> EstimateSet     produce the grid of intervals
    EstimateSet.meets_target(eps)                 may I stop now?

A deliberate design choice: ``meets_target`` returns False when any cell has no
interval at all (empty group, single observation, zero-valued estimate). An
unresolved cell is not a satisfied cell, and treating it as one is how a
sampling controller convinces itself it has converged when it has not.

Not handled here
----------------
Repeatedly calling ``meets_target`` across sampling iterations is *optional
stopping*, and fixed-sample intervals are not valid under it: stopping the
moment the interval looks narrow selects for narrowness, so the reported
coverage is optimistic. Making the intervals anytime-valid is a separate piece
of work (confidence sequences); until it lands, the coverage levels reported
here are correct for a single look and optimistic for a stopping rule. That
limitation is deliberate, documented, and must not be quietly forgotten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from backend.stats.contracts import (
    Aggregate,
    Correction,
    Estimate,
    Method,
    SampleStats,
)
from backend.stats.estimators import DEFAULT_COVERAGE_LEVEL, estimate_aggregate
from backend.stats.intervals import adjust_coverage_level


@dataclass(frozen=True)
class AggregateCell:
    """
    One cell of the result grid: one aggregate, over one group, with the
    sufficient statistics observed for it in this sample.

    ``group_key`` is None for an ungrouped query, otherwise the tuple of
    GROUP BY column values. ``alias`` is the output column name, carried
    through so the controller can map estimates back onto result columns.
    """

    alias: str
    aggregate: Aggregate
    stats: SampleStats
    group_key: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class EstimateSet:
    """
    Every interval for one sampled query result, at a stated coverage level.

    Attributes
    ----------
    estimates:
        ``{group_key: {alias: Estimate}}``. Ungrouped queries use the single
        key ``None``.
    per_interval_coverage:
        The level each individual interval was computed at. Under a
        multiplicity correction this is *higher* than the requested level --
        each interval is made wider so the family as a whole holds.
    family_wise_coverage:
        The level at which *all* intervals hold simultaneously, or None when
        no correction was applied and only marginal coverage is claimed.
    """

    estimates: dict[Any, dict[str, Estimate]]
    per_interval_coverage: float
    family_wise_coverage: float | None
    correction: Correction
    num_intervals: int
    notes: tuple[str, ...] = field(default_factory=tuple)

    def flat(self) -> list[Estimate]:
        """All estimates in a single list, group structure discarded."""
        return [
            estimate
            for by_alias in self.estimates.values()
            for estimate in by_alias.values()
        ]

    @property
    def unresolved(self) -> list[tuple[Any, str]]:
        """(group_key, alias) pairs for which no interval could be formed."""
        return [
            (group_key, alias)
            for group_key, by_alias in self.estimates.items()
            for alias, estimate in by_alias.items()
            if estimate.relative_half_width is None
        ]

    @property
    def max_relative_half_width(self) -> float | None:
        """
        Widest relative half-width across all resolved cells.

        This is the number a controller compares against its error target: the
        query is only as accurate as its worst cell. None when nothing resolved.
        """
        widths = [
            estimate.relative_half_width
            for estimate in self.flat()
            if estimate.relative_half_width is not None
        ]
        return max(widths) if widths else None

    def meets_target(self, target_relative_error: float) -> bool:
        """
        True only when every cell has an interval and every one is tight enough.

        Unresolved cells count as failures, not as passes. A group that has not
        been observed yet cannot be declared accurate.
        """
        if not self.estimates:
            return False
        if self.unresolved:
            return False
        widest = self.max_relative_half_width
        if widest is None:
            return False
        return widest <= target_relative_error

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form for the API layer."""
        return {
            "per_interval_coverage": self.per_interval_coverage,
            "family_wise_coverage": self.family_wise_coverage,
            "correction": self.correction.value,
            "num_intervals": self.num_intervals,
            "max_relative_half_width": self.max_relative_half_width,
            "unresolved": [
                {"group_key": list(key) if isinstance(key, tuple) else key, "alias": alias}
                for key, alias in self.unresolved
            ],
            "estimates": [
                {
                    "group_key": (
                        list(group_key) if isinstance(group_key, tuple) else group_key
                    ),
                    "alias": alias,
                    **estimate.to_dict(),
                }
                for group_key, by_alias in self.estimates.items()
                for alias, estimate in by_alias.items()
            ],
            "notes": list(self.notes),
        }


def estimate_query(
    cells: Sequence[AggregateCell] | Iterable[AggregateCell],
    coverage_level: float = DEFAULT_COVERAGE_LEVEL,
    method: Method | Callable[[AggregateCell], Method] = Method.CLT,
    correction: Correction = Correction.BONFERRONI,
    *,
    fallback_to_clt: bool = True,
) -> EstimateSet:
    """
    Estimate every cell of a sampled query result at a family-wise coverage level.

    With a correction applied, ``coverage_level`` is interpreted as the
    probability that *all* intervals hold simultaneously, and each individual
    interval is widened to pay for that. With ``Correction.NONE`` it is the
    marginal per-interval level and no simultaneous claim is made.

    ``method`` may be a single ``Method`` applied to every cell, or a callable
    ``cell -> Method`` so a grid can keep the normal approximation for the
    cells that support it and use a finite-sample bound only where the skew
    demands it.

    The number of intervals is counted from the cells actually supplied, which
    means it grows as sampling discovers new groups. A correction computed on a
    partially-discovered group set is not valid for the final one; the returned
    notes say so when the risk is present.
    """
    method_for = method if callable(method) else (lambda _cell: method)
    cell_list = list(cells)
    if not cell_list:
        return EstimateSet(
            estimates={},
            per_interval_coverage=coverage_level,
            family_wise_coverage=None,
            correction=correction,
            num_intervals=0,
            notes=("no cells supplied; nothing to estimate",),
        )

    num_intervals = len(cell_list)
    per_interval = adjust_coverage_level(coverage_level, num_intervals, correction)

    notes: tuple[str, ...] = ()
    if correction is Correction.NONE and num_intervals > 1:
        notes = notes + (
            f"{num_intervals} intervals reported at marginal coverage with no "
            "multiplicity correction; they do not hold simultaneously",
        )
    if correction is Correction.SIDAK:
        notes = notes + (
            "Sidak correction assumes independence across intervals; aggregates "
            "sharing one sample are correlated, so the family-wise level is "
            "approximate",
        )
    grouped_cells = any(cell.group_key is not None for cell in cell_list)
    if grouped_cells:
        notes = notes + (
            "the correction is sized to the groups observed so far; groups not "
            "yet seen in the sample are not covered by the family-wise claim",
        )

    estimates: dict[Any, dict[str, Estimate]] = {}
    for cell in cell_list:
        estimate = estimate_aggregate(
            cell.aggregate,
            cell.stats,
            per_interval,
            method_for(cell),
            fallback_to_clt=fallback_to_clt,
        )
        estimates.setdefault(cell.group_key, {})[cell.alias] = estimate

    return EstimateSet(
        estimates=estimates,
        per_interval_coverage=per_interval,
        family_wise_coverage=(
            coverage_level if correction is not Correction.NONE else None
        ),
        correction=correction,
        num_intervals=num_intervals,
        notes=notes,
    )
