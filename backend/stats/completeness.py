"""
Group completeness: how much of the GROUP BY result is missing?

Every interval in this package answers "how accurate is this number?". None of
them answers the prior question: "are all the numbers here?"

A sampled ``GROUP BY`` returns only the groups that happened to appear in the
sample. A query over 25 nations that samples 1% may return 19 of them, and
nothing in the result says the other six exist. The rows that are shown may
each be perfectly estimated while the answer as a whole is wrong, because it is
incomplete -- and incompleteness is invisible to a caller looking only at the
rows returned. That is a different failure mode from a wide interval, and it
needs its own estimate.

This is the classical unseen-species problem, and its two standard estimators
apply directly, using only the per-group sample counts the controller already
has:

    Chao1        a lower bound on the true number of groups, driven by how many
                 groups were seen exactly once and exactly twice. The intuition
                 is that a sample full of singletons is a sample that is still
                 finding new groups, so many more remain; a sample with no
                 singletons has probably found nearly everything.

    Good-Turing  the share of the *population* that belongs to groups that were
                 observed. Distinct from the share of groups: missing twenty
                 tiny groups may cost almost no mass, while missing one large
                 one is serious. Both numbers are reported because a caller
                 needs to know which kind of incompleteness it has.

Assumptions, and where they fail here
-------------------------------------
Both estimators assume individuals are drawn independently -- the multinomial
model. Engine-native ``TABLESAMPLE SYSTEM`` violates that badly: under block
sampling on clustered data a whole group can live inside one physical block, so
it is either fully present or fully absent, and the singleton counts these
estimators depend on are no longer meaningful. Under those conditions the
numbers here are optimistic and should be read as a floor on the problem rather
than a measurement of it. ``design_effect.py`` documents the same limitation
for variance.

References: Good (1953) on population frequencies of species; Chao (1984) on
non-parametric estimation of the number of classes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class GroupCompleteness:
    """
    An assessment of what a sampled GROUP BY may be missing.

    ``estimated_total_groups`` is a *lower* bound in expectation, not a point
    estimate: Chao1 systematically understates when many groups are rare, so
    "at least this many exist" is the only honest reading.
    """

    observed_groups: int
    singletons: int
    doubletons: int
    total_sampled_rows: int
    estimated_total_groups: float
    estimated_missing_groups: float
    sample_coverage: float | None
    notes: tuple[str, ...] = ()

    @property
    def looks_complete(self) -> bool:
        """
        Whether the sample plausibly found every group.

        Requires both that Chao1 predicts nothing substantial missing and that
        no singletons remain. Singletons are the signal that discovery is still
        in progress: while the sample is still turning up groups exactly once,
        there are almost certainly more it has not turned up at all.
        """
        return self.singletons == 0 and self.estimated_missing_groups < 0.5

    @property
    def missing_mass(self) -> float | None:
        """Estimated share of the population in groups never observed."""
        if self.sample_coverage is None:
            return None
        return max(0.0, 1.0 - self.sample_coverage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_groups": self.observed_groups,
            "singletons": self.singletons,
            "doubletons": self.doubletons,
            "total_sampled_rows": self.total_sampled_rows,
            "estimated_total_groups": self.estimated_total_groups,
            "estimated_missing_groups": self.estimated_missing_groups,
            "sample_coverage": self.sample_coverage,
            "missing_mass": self.missing_mass,
            "looks_complete": self.looks_complete,
            "notes": list(self.notes),
        }


def chao1(singletons: int, doubletons: int, observed_groups: int) -> float:
    """
    Chao1 lower bound on the true number of groups.

    The bias-corrected form is used throughout::

        S_est = S_obs + f1 * (f1 - 1) / (2 * (f2 + 1))

    It is preferred over the classical ``S_obs + f1^2 / (2 * f2)`` because that
    form divides by the doubleton count and so is undefined -- and in practice
    explosive -- whenever no group was seen exactly twice, which happens
    constantly at small sample sizes. The corrected version degrades gracefully
    instead.
    """
    if singletons < 0 or doubletons < 0:
        raise ValueError("frequency counts must be non-negative")
    return observed_groups + singletons * (singletons - 1) / (2.0 * (doubletons + 1))


def good_turing_coverage(singletons: int, total_sampled_rows: int) -> float | None:
    """
    Good-Turing estimate of the population share held by observed groups.

    ``C = 1 - f1 / n``. The insight is that the rate at which singletons appear
    estimates the rate at which the *next* draw would find something new, so the
    singleton share estimates the mass that has been missed.

    Returns None for an empty sample, where the quantity is undefined rather
    than zero.
    """
    if total_sampled_rows <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - singletons / total_sampled_rows))


def assess_completeness(
    group_counts: Mapping[Any, int] | Sequence[int],
    *,
    design_effect: float = 1.0,
    known_total_groups: int | None = None,
) -> GroupCompleteness:
    """
    Assess how much of a grouped result the sample is likely missing.

    ``group_counts`` is the number of sampled rows falling in each observed
    group -- available directly from the same pushed-down GROUP BY that
    produced the estimates. Groups with a count of zero are ignored: a group
    that was not observed cannot contribute evidence about how many were not
    observed.

    Pass ``known_total_groups`` when the true cardinality is known from a
    dimension table or a constraint. It is far better than any estimate, and
    supplying it turns this from an inference into a measurement.
    """
    if isinstance(group_counts, Mapping):
        counts = [count for count in group_counts.values() if count > 0]
    else:
        counts = [count for count in group_counts if count > 0]

    observed = len(counts)
    total_rows = sum(counts)
    singletons = sum(1 for count in counts if count == 1)
    doubletons = sum(1 for count in counts if count == 2)

    notes: tuple[str, ...] = ()

    if known_total_groups is not None:
        missing = max(0, known_total_groups - observed)
        return GroupCompleteness(
            observed_groups=observed,
            singletons=singletons,
            doubletons=doubletons,
            total_sampled_rows=total_rows,
            estimated_total_groups=float(known_total_groups),
            estimated_missing_groups=float(missing),
            sample_coverage=good_turing_coverage(singletons, total_rows),
            notes=(
                f"group cardinality supplied by the caller ({known_total_groups}); "
                f"{missing} group(s) are definitely absent from this sample",
            ),
        )

    if observed == 0:
        return GroupCompleteness(
            observed_groups=0,
            singletons=0,
            doubletons=0,
            total_sampled_rows=0,
            estimated_total_groups=0.0,
            estimated_missing_groups=float("inf"),
            sample_coverage=None,
            notes=(
                "no groups observed at all; the number missing is unbounded and "
                "nothing can be inferred from an empty sample",
            ),
        )

    estimated_total = chao1(singletons, doubletons, observed)
    coverage = good_turing_coverage(singletons, total_rows)

    if singletons == 0:
        notes = notes + (
            "no group was seen exactly once, which is the strongest available "
            "signal that discovery has finished",
        )
    elif singletons > observed / 2:
        notes = notes + (
            f"{singletons} of {observed} observed groups appeared exactly once; "
            "the sample is still finding new groups and the estimate below is "
            "very likely an underestimate",
        )

    if design_effect > 1.5:
        notes = notes + (
            f"design effect is {design_effect:.1f}, so rows are not independent "
            "draws; under block sampling a whole group can sit inside one block "
            "and be all-or-nothing, which makes these singleton-based estimates "
            "optimistic -- read them as a floor on what is missing",
        )

    return GroupCompleteness(
        observed_groups=observed,
        singletons=singletons,
        doubletons=doubletons,
        total_sampled_rows=total_rows,
        estimated_total_groups=estimated_total,
        estimated_missing_groups=max(0.0, estimated_total - observed),
        sample_coverage=coverage,
        notes=notes
        + (
            "Chao1 is a lower bound in expectation, so the true group count is "
            "at least this, not exactly this",
        ),
    )
