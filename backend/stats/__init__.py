"""
Statistical estimation for AetherQuery approximate query processing.

Replaces the legacy ``estimate_confidence`` heuristic in
``backend.core.runtime_sampling``, which reports how much an answer moved
between sampling iterations. Iteration-to-iteration stability is not accuracy:
a consistently biased estimator is perfectly stable and reports high
confidence while being wrong. Nothing in that number supports a probability
statement, so it cannot be called a confidence level.

What this package provides instead is a genuine confidence interval: an
estimate, its variance, bounds at a stated coverage level, and the method that
produced them.

Usage::

    from backend.stats import Aggregate, SampleStats, estimate_aggregate

    stats = SampleStats(
        n_sample=50_000,        # rows in the sample, before WHERE/GROUP BY
        n_domain=3_120,         # sampled rows in this group
        sum_x=48_204.5,
        sum_xx=921_884.2,
        min_x=0.0,
        max_x=104.0,
        population_size=1_000_000,
    )
    estimate = estimate_aggregate(Aggregate.SUM, stats)
    print(estimate.estimate, estimate.ci_low, estimate.ci_high)

For a whole result grid with multiplicity handled across groups, use
``estimate_query`` from this package instead of calling the single-cell
estimators directly.

Scope and limitations, stated up front
--------------------------------------
* Intervals are valid for a *single look* at the data. Using them in a
  repeated-peeking stopping rule is optional stopping and makes the reported
  coverage optimistic. Anytime-valid confidence sequences are separate work.
* ``design_effect`` defaults to 1.0, which assumes simple random sampling.
  Engine-native ``TABLESAMPLE SYSTEM`` is block sampling, and on physically
  clustered data its true design effect can be very large. Leaving the default
  in place while sampling with SYSTEM will under-state error. Measuring it is
  separate work.
* Joins are out of scope. Scaling a one-sided join sample by 1/f is unbiased
  only under conditions this library cannot verify, and join-key multiplicity
  adds variance none of these formulas capture.
"""

from backend.stats.api import (
    AggregateCell,
    EstimateSet,
    estimate_query,
)
from backend.stats.completeness import (
    GroupCompleteness,
    assess_completeness,
)
from backend.stats.contracts import (
    Aggregate,
    Correction,
    Estimate,
    Method,
    SampleStats,
)
from backend.stats.design_effect import (
    BlockAggregate,
    ClusterSampleStats,
    estimate_clustered,
    estimate_design_effect,
    intraclass_correlation,
    minimum_blocks_for_valid_interval,
    project_design_effect,
)
from backend.stats.selection import (
    MethodRecommendation,
    observations_needed,
    recommend_method,
)
from backend.stats.sequential import (
    Schedule,
    SequentialAnalysis,
    SequentialResult,
    price_of_peeking,
)
from backend.stats.estimators import (
    DEFAULT_COVERAGE_LEVEL,
    estimate_aggregate,
    estimate_avg,
    estimate_count,
    estimate_sum,
)
from backend.stats.intervals import (
    adjust_coverage_level,
    clt_half_width,
    empirical_bernstein_half_width,
    hoeffding_half_width,
    normal_quantile,
    rule_of_three_upper_bound,
)

__all__ = [
    "DEFAULT_COVERAGE_LEVEL",
    # contracts
    "Aggregate",
    "AggregateCell",
    "Correction",
    "Estimate",
    "EstimateSet",
    "Method",
    "SampleStats",
    # single-cell estimation
    "estimate_aggregate",
    "estimate_avg",
    "estimate_count",
    "estimate_sum",
    # query-level estimation and multiplicity
    "estimate_query",
    "adjust_coverage_level",
    # interval methods
    "clt_half_width",
    "empirical_bernstein_half_width",
    "hoeffding_half_width",
    "normal_quantile",
    "rule_of_three_upper_bound",
    # block sampling
    "BlockAggregate",
    "ClusterSampleStats",
    "estimate_clustered",
    "estimate_design_effect",
    "intraclass_correlation",
    "minimum_blocks_for_valid_interval",
    "project_design_effect",
    # sequential / anytime-valid
    "Schedule",
    "SequentialAnalysis",
    "SequentialResult",
    "price_of_peeking",
    # method selection
    "MethodRecommendation",
    "observations_needed",
    "recommend_method",
    # group completeness
    "GroupCompleteness",
    "assess_completeness",
]
