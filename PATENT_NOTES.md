# AetherQuery — Patent Notes

**Status:** support material for a provisional filing. Not legal advice; have a
patent attorney run the prior-art search and draft the claims. This document
records what is *implemented and tested* so claims match the system, not
aspiration.

**Date:** 2026-08-30

---

## 1. What the system does

AetherQuery answers `COUNT` / `SUM` / `AVG` SQL queries approximately by drawing
progressively larger engine-native samples and stopping the instant a
statistically valid confidence interval over the **entire result grid** (every
`GROUP BY` group × every aggregate) is within a caller-supplied relative-error
target ε. The sufficient statistics for every cell are computed in **one
pushed-down query per sample**. The next sample size is chosen from how far the
widest interval still is from ε. For fact→dimension joins the sampling unit is
the fact table's physical row group, so the between-block variance absorbs the
1:N fan-out; the starting rate is chosen from HyperLogLog sketches of the join
keys; and a group-completeness monitor blocks a "converged" answer while
`GROUP BY` groups are still being discovered.

Measured (end-to-end, `scripts/run_engine_coverage_study.py`): 95–100 %
empirical interval coverage at an explicit accuracy target, single-table and
fact→dimension join, on TPC-H (SF1/SF10) and on a synthetic Pareto-tailed
dataset (skewness ≈ 29), at 1.3–13× speedup over exact. Under pathological skew
(skewness ≈ 440) the engine degrades to an exact scan rather than report an
interval it cannot back up.

---

## 2. Candidate independent claims (all implemented and tested)

### Claim A — CI-grid adaptive stopping

A method for approximate query processing comprising:

1. receiving an aggregate query and a target relative error ε;
2. drawing a sample of a relation at fraction *f* and computing, **in a single
   pushed-down aggregate query**, sufficient statistics per (group, aggregate)
   cell — the pre-predicate sample size, the in-domain count (via a per-cell
   `FILTER`, so the sampling denominator is recoverable in the same pass), and
   the domain sum, sum of squares, sum of cubes, variance and value range;
3. forming, for every cell simultaneously, a confidence interval at a
   family-wise coverage level using a multiplicity correction sized to the
   number of groups discovered so far, choosing per grid between a
   normal-approximation and a finite-sample bound from the worst cell's skew;
4. **terminating** when the widest *relative half-width* across all cells is
   ≤ ε, and otherwise increasing *f* by a factor that grows with the ratio of
   the widest half-width to ε;
5. returning the point estimates with their intervals and the coverage level
   actually achieved.

Implemented: `backend/core/sufficient_stats.py` (`fetch_sufficient_stats`,
`evaluate_sample_accuracy`), `backend/core/runtime_sampling.py`
(`use_ci_stop` path). Tested: `backend/tests/test_ci_stop.py`,
`scripts/run_engine_coverage_study.py`.

*Distinguish from* online aggregation (Hellerstein 1997) and BlinkDB: those
stop on a single-aggregate interval or a workload-learned error profile; here
the stop is gated on the **worst cell of a multiplicity-corrected grid**, the
correction is re-sized as groups are discovered, the sufficient statistics for
the whole grid come from one pushed-down pass, and the step size is a function
of the interval-to-target ratio.

### Claim B — cluster-sampling intervals for fact→dimension joins

A method for forming a confidence interval over an approximate `GROUP BY`
aggregate of an INNER equi-join comprising:

1. sampling only the fact (first) relation with an engine-native row-group
   sampler and joining the sample against the un-sampled dimension relations;
2. grouping the joined result by the fact relation's **physical row-group
   identifier** (e.g. `rowid` integer-divided by the engine's vector size) in
   addition to the query's `GROUP BY`, and reading per (row-group, group) the
   row count, in-domain count, and column sum / sum of squares in one pass;
3. treating each fact row-group as one sampling unit, so that the estimator
   variance is the sample variance *of the per-row-group totals* and the
   interval's degrees of freedom are the number of sampled row-groups, not the
   number of joined rows;
4. applying a family-wise multiplicity correction across the grid and
   terminating on the same worst-relative-half-width rule as Claim A.

Implemented: `backend/core/sufficient_stats.py`
(`build_join_block_stats_sql`, `evaluate_join_sample_accuracy`,
`join_ci_is_defensible`), on top of
`backend.stats.design_effect.ClusterSampleStats` / `estimate_clustered`.
Tested: `backend/tests/test_ci_stop.py::test_join_sample_accuracy_uses_cluster_estimator`.

*Why it matters:* the naive alternative — expand a one-sided join sample by
1/f and use simple-random-sampling variance — was implemented and **measured to
under-cover at 0–60 % vs a nominal 95 %** on 1:N joins, because a 1:N join
sample is cluster-sampled and its design effect is large. Using the fact
row-group as the unit restores nominal coverage (measured 95–100 % at SF10,
88–92 % on a ~70-block fact table at SF1).

### Claim C — HyperLogLog-guided JOIN start rate

A method for choosing the initial sample fraction of an approximate JOIN
comprising: probing the fact relation's equi-join key, sketching its
cardinality with a HyperLogLog register array and scaling to the full
relation; estimating the output-group count by resolving the `GROUP BY` key
against every relation in the query (sampling the fact relation, counting
dimension keys exactly); and selecting the fraction that yields a target number
of matched rows per output group, clamped between a floor that scales with the
join-predicate count and a ceiling below a full scan.

Implemented: `backend/core/join_sampling.py`
(`hll_guided_join_min_rate`, `_join_key_columns`). Tested:
`backend/tests/test_hll_join_rate.py`. Reported on the payload as
`join_rate_selection`.

### Claim D — group-completeness escalation for grouped JOINs

A method for approximate execution of a grouped JOIN comprising: tracking the
distinct-group count across sampling iterations; classifying a sample as
*incomplete* when it returns no groups, fewer than a previous larger sample, or
a still-growing set; while incomplete, suppressing every convergence- and
time-based stop and forcing a larger fraction; bounding the number of forced
increases; and, past a group-count ceiling at which sampling cannot complete,
terminating early and **labelling the result incomplete** rather than
converged.

Implemented: `backend/core/runtime_sampling.py` (`groups_incomplete`,
`completeness_blocks_stop`, `max_incomplete_iterations`, `aqp_group_ceiling`).
Tested: `backend/tests/test_runtime_join_guard.py`.

### Dependent-claim material

* Recognising an unfiltered, ungrouped `COUNT(*)` as the relation cardinality
  and returning it with a zero-width interval (no sampling).
* Using the *realized* sampling fraction n/N (from a cached `COUNT(*)`) rather
  than the nominal `TABLESAMPLE` percentage, removing the block sampler's
  nominal-vs-actual scaling bias.
* Deriving ε from a user "accuracy target" as `1 − target/100`.
* Inflating the design effect of `SUM`/`AVG` interval variance by a constant
  (implemented: 1.75), leaving `COUNT` at 1.0.
  (`SYSTEM_SAMPLING_DESIGN_EFFECT`.)
* Anytime-valid stopping via a harmonic alpha-spending schedule over looks, so certification holds under any stopping rule despite independent re-draws.
* Degrading to an exact scan, rather than reporting a tight interval, when the
  interval never reaches ε within the sampling budget.

---

## 3. Known weak points to disclose / design around

* **General joins.** Claims B/D cover INNER fact→dimension shapes. LEFT/RIGHT/
  FULL and M:N joins keep the completeness heuristic (Claim D) and get **no CI**
  — the cluster-unit argument needs the fact row to map to ≤ a bounded cluster.
* **Small fact tables.** Cluster-estimator joins need enough fact row-groups;
  below ~100 (TPC-H SF1 `customer` ≈ 73) coverage on a 1:N join dips to ~88 %.
* **Constant design effect.** 1.75 is a stand-in for a per-query measurement
  (the same block-aggregate machinery Claim B uses could measure it).
* **HLL start-rate is advisory** — the completeness monitor and CI stop are
  what bound the error; the HLL rate only affects how fast they get there.

---

## 4. Filing posture

File a **provisional now** covering Claims A–D and the dependent material, with
this repository as the enabling disclosure. Within the 12-month window: measure
the design effect per query (removing the 1.75 constant), extend the cluster
argument to a bounded class of M:N joins or explicitly scope them out, and add
a workload-level evaluation. Counsel should search prior art against
online/adaptive aggregation, BlinkDB, WanderJoin, Sample+Seek, Quickr, Pulse,
ApproxHadoop, IDEA/incremental-viz stopping rules, and DuckDB/`TABLESAMPLE`
sampling.
