# AetherQuery — Patent Notes

**Status:** draft support material for a provisional filing. Not legal advice;
have a patent attorney do the prior-art search and claim drafting. This
document records what is *implemented and tested* so claims can be written to
match the system rather than to aspiration.

**Date:** 2026-08-30

---

## 1. What the system does (one paragraph)

AetherQuery answers `COUNT` / `SUM` / `AVG` SQL queries approximately by drawing
progressively larger engine-native samples and stopping as soon as a
statistically valid confidence interval over the *entire result grid* (every
GROUP BY group × every aggregate) is within a caller-supplied relative-error
target. Sample sizes are chosen adaptively from the observed interval width;
for JOIN queries the starting rate is chosen from HyperLogLog cardinality
sketches of the join keys, and a group-completeness monitor prevents the engine
from reporting a confident answer while GROUP BY groups are still missing.

---

## 2. Candidate independent claims (mechanism-level, implemented)

### Claim A — CI-grid adaptive stopping

A method for approximate query processing comprising:
1. receiving an aggregate query and a target relative error ε;
2. drawing a sample of a relation at fraction f and computing, **in a single
   pushed-down aggregate query**, sufficient statistics per (group, aggregate)
   cell: the pre-predicate sample size, the in-domain count, and the domain
   sum, sum-of-squares, sum-of-cubes, variance, skewness, and value range;
3. forming, for every cell simultaneously, a confidence interval at a
   family-wise coverage level using a multiplicity correction across the number
   of groups discovered so far;
4. **terminating** when the widest *relative half-width* across all cells is
   ≤ ε, and otherwise increasing f by an amount that grows as the ratio of the
   widest half-width to ε;
5. returning the point estimates together with the intervals and the coverage
   level actually achieved.

Implemented: `backend/core/sufficient_stats.py`,
`backend/core/runtime_sampling.py` (`use_ci_stop` path). Tested:
`backend/tests/test_ci_stop.py`, `scripts/run_engine_coverage_study.py`
(measured coverage 93–100 % at SF10 with an explicit target).

*Distinguish from* online aggregation (Hellerstein 1997) and BlinkDB: those
stop on a single-aggregate interval or a pre-computed error profile; here the
stop is gated on the **worst cell of a multiplicity-corrected grid**, with the
correction re-sized as groups are discovered, and the step size is a function
of the interval-to-target ratio.

### Claim B — HyperLogLog-guided JOIN start rate

A method for choosing the initial sample fraction for an approximate JOIN
query comprising:
1. drawing a small probe of the primary relation's equi-join key column;
2. inserting the probed values into a HyperLogLog sketch and scaling its
   cardinality estimate to the full relation;
3. estimating the number of output groups by the same sketch technique;
4. selecting the initial fraction as the one that yields a target number of
   matched rows per output group, clamped to a floor that scales with the
   number of join predicates and a ceiling below a full scan;
5. proceeding with adaptive progression from that fraction.

Implemented: `backend/core/join_sampling.py`
(`hll_guided_join_min_rate`, `_join_key_columns`). Tested:
`backend/tests/test_hll_join_rate.py`. Reported on the result payload as
`join_rate_selection`.

*Distinguish from* fixed-fraction and geometric-ladder samplers: the start
point is derived from a cardinality sketch of the actual join keys, not a
constant.

### Claim C — group-completeness escalation for grouped JOINs

A method for approximate execution of a `GROUP BY` query over a JOIN
comprising: tracking, across sampling iterations, the number of distinct groups
returned; classifying the current sample as *incomplete* when it returns no
groups, fewer groups than a previous larger sample, or a still-growing group
set; while incomplete, suppressing every convergence- and time-based stopping
rule and forcing an increased sample fraction; bounding the number of such
forced increases; and, when the group count exceeds a ceiling at which
sampling cannot complete, terminating early and **labelling the result as
incomplete** rather than returning it as converged.

Implemented: `backend/core/runtime_sampling.py`
(`groups_incomplete`, `completeness_blocks_stop`, `aqp_group_ceiling`). Tested:
`backend/tests/test_runtime_join_guard.py`.

*Distinguish from* prior grouped-AQP work, which either assumes all groups are
observed or silently drops unobserved ones.

### Dependent-claim material

* Recognising an unfiltered, ungrouped `COUNT(*)` as the relation cardinality
  and returning it with a zero-width interval (no sampling).
* Choosing the interval method per grid from the worst-skewed cell: CLT when
  Cochran's rule is satisfied, a finite-sample (empirical-Bernstein) bound
  otherwise.
* Using the *realized* sampling fraction n/N (from a cached `COUNT(*)`) rather
  than the nominal `TABLESAMPLE` percentage, to remove the block-sampler's
  nominal-vs-actual bias.
* Deriving the error budget from a user "accuracy target" as `1 − target/100`.
* Inflating the design effect of `SUM`/`AVG` interval variance by a constant
  factor (implemented: 1.5) to account for engine-native row-group
  (`TABLESAMPLE SYSTEM`) sampling, while leaving `COUNT` at 1.0. Measured to
  lift grouped-`SUM` coverage from ~86 % to ~95 % at a nominal-95 % target.
  (`backend/core/sufficient_stats.py:SYSTEM_SAMPLING_DESIGN_EFFECT`.)
* HLL group-count probe that resolves the `GROUP BY` key against every table in
  a join, sampling the fact table but counting dimension keys exactly.

---

## 3. Known weak points to disclose / design around

* **JOIN aggregate estimator is heuristic.** Scaling a one-sided join sample by
  1/f is not unbiased for many join shapes; the system currently pairs it with
  the completeness monitor (Claim C) rather than a proven estimator. A claim to
  "unbiased approximate joins" is **not** currently supported. Routing joins
  through the CI machinery (Claim A) was implemented and measured to under-cover
  at 0–60 % vs a nominal 95 % on 1:N joins (cluster-sampling design effect), and
  reverted. A defensible join claim needs a cluster-sampling estimator
  (`backend.stats.design_effect.ClusterSampleStats`) fed by block-level
  aggregates.
* **HLL group-count probe is weak** when the GROUP BY key is not on the primary
  relation (falls back to 1). Narrow Claim B or improve the probe first.
* **Grouped-SUM coverage** on heavy-tailed columns is ~88 % at the loose
  default budget (nominal at an explicit target). Disclose as a limitation.

---

## 4. Recommended filing posture

File a **provisional now** covering Claims A–C plus the dependent material, with
this repository state as the enabling disclosure. Use the 12-month window to:
tighten the HLL group probe, engage the finite-sample interval method for
skewed grouped SUM (close the coverage gap), and decide whether to pursue a
JOIN estimator claim or drop joins from the non-provisional. Have counsel run
the prior-art search against: online/adaptive aggregation, BlinkDB, WanderJoin,
Sample+Seek, Quickr, Pulse, ApproxHadoop, and DuckDB's own sampling.
