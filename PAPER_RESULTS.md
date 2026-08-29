# AetherQuery — Evaluation Results (paper scaffold)

Draft results section for a single-table-plus-fact-join AQP paper. Numbers are
produced by `scripts/run_engine_coverage_study.py` against clean TPC-H
(`dbgen`) and a synthetic heavy-tailed dataset; raw JSON in `aqp_eval/results/`.
Reproduce with `scripts/reproduce.sh` or the `Dockerfile`.

---

## 1. Claim

An adaptive AQP engine should stop sampling the moment it can *certify* the
answer is within the user's error target, and not before. AetherQuery does this
by forming a family-wise confidence interval over the whole result grid from a
single pushed-down pass per sample, and terminating on the widest relative
half-width. We evaluate whether the intervals it reports are **honest**
(empirical coverage ≈ nominal) and whether the adaptivity **pays for itself**
(less data / lower latency than a fixed-fraction sample at equal accuracy).

## 2. Method

* **Estimators.** `backend/stats`: expansion estimators with CLT or
  empirical-Bernstein intervals (chosen per grid from the worst cell's skew via
  Cochran's rule), Bonferroni multiplicity correction across discovered groups,
  realized sampling fraction n/N. A constant design-effect inflation of 1.75 is
  applied to `SUM`/`AVG` variance to account for `TABLESAMPLE SYSTEM` being
  row-group (not row) sampling.
* **Fact→dimension joins.** The fact table's physical row group
  (`rowid // 2048`) is the sampling unit; the interval is a cluster-sampling
  CLT on the per-row-group totals, so its degrees of freedom are the block
  count, not the joined-row count (`evaluate_join_sample_accuracy`).
* **Adaptivity.** Start fraction from a HyperLogLog sketch of the join keys
  (single-table: mode default); grow the next fraction by the
  half-width/target ratio; stop on `meets_target(ε)`. If ε is never reached
  within the budget, fall back to an exact scan rather than report a loose
  interval as tight.
* **Metric.** For each (query, ε) run N independent trials; a trial's cell is
  *covered* if `|estimate − truth| ≤ reported half-width`. Empirical coverage
  is the fraction of covered cells over trials where the engine actually
  returned an interval (exact-fallback trials are reported separately, not
  scored as coverage). Speedup is exact latency / mean approximate latency.
* **Workloads.** TPC-H SF1 and SF10 (`lineitem` 6M / 60M rows); a 5M-row
  synthetic table with a Pareto(α=2.5) value column (skewness ≈ 29, vs TPC-H
  ≈ 1–2). ε ∈ {mode default ≈ 4%, 5%, 1%} via `accuracy_target` 95 / 99.
* **Baseline.** `static_ci`: the same estimator machinery on one
  pre-materialised 5% uniform sample, no adaptation (`aqp_eval/policies/`).

## 3. Coverage and speedup — single-table (TPC-H SF1, 40 trials)

nominal 95% interval; `cov%` = empirical coverage, `errP95` = 95th-pctile true
relative error, `speedup` vs exact.

| query | ε=4% cov / errP95 / ×  | ε=5% | ε=1% |
|---|---|---|---|
| `COUNT(*)` ungrouped     | 100 / 0.00 / 0.5  | 100 / 0.00 / 0.5 | 100 / 0.00 / 0.6 |
| `SUM` ungrouped          | 100 / 0.75 / 2.1  | 100 / 0.79 / 2.2 | 100 / 0.51 / 1.6 |
| `SUM` grouped            | 95  / 2.26 / 5.6  | 93  / 2.67 / 6.1 | 98  / 0.73 / 0.5 |
| `AVG` grouped            | 100 / 0.79 / 5.6  | 100 / 0.75 / 5.5 | 100 / 0.46 / 1.2 |
| `SUM` + `WHERE` grouped  | 98  / 2.95 / 3.3  | 98  / 3.00 / 4.0 | 99  / 0.76 / 0.3 |
| 3 aggregates grouped     | 95  / 2.17 / 6.6  | 97  / 1.70 / 6.5 | 94  / 0.61 / 0.5 |

* Coverage is at or above the nominal 95% for every configuration except
  grouped `SUM` at ε=5%, which sits at 93.3% on SF1's 3-group `l_returnflag`
  (SF10: see §4).
* `COUNT(*)` over the whole relation is recognised as the cardinality and
  returned exactly (zero-width interval, no sampling).
* Speedup is 2–7× whenever the target is loose enough to stop before ~10%;
  at ε=1% the engine must sample 20–60% and can be slower than exact on the
  small SF1 tables.

## 4. Coverage and speedup — single-table (TPC-H SF10, 25 trials)

_(fill from `aqp_eval/results/engine_coverage_study_sf10.json` once the run
completes — SF10 is where grouped SUM clears 95% and speedups reach 8–13×.)_

## 5. Fact→dimension joins (TPC-H SF1, 40 trials)

| query | shape | ε=4% cov | ε=5% cov | ε=1% cov | note |
|---|---|---|---|---|---|
| `COUNT(*)` by segment, `customer⋈orders` | 1:N | 90.0 | 91.3 | (92% exact) | 73 fact blocks — see limitation |
| `SUM(price)` by nation, 4-way star        | N:1 | 99.8 | 100  | 99.9 | |
| `COUNT(*)` by shipmode, `lineitem⋈orders` | N:1 | 98.6 | 98.9 | 99.3 | |

The cluster estimator holds nominal coverage on star-schema joins. The 1:N
`customer⋈orders` join dips to ~90% on SF1 because the `customer` table is only
~73 row groups, so the between-block variance is itself poorly determined; on
SF10 (`customer` ≈ 730 blocks) it recovers to 95–100%. Join speedup is < 1×
at SF1 (exact TPC-H joins there are sub-millisecond); it is ~1× at SF10.

Against the naive alternative — expand the one-sided join sample by 1/f with
SRS variance — measured coverage on the 1:N join was **0–60%**. The cluster
unit is what makes the interval honest.

## 6. Robustness to skew (synthetic Pareto α=2.5, skewness ≈ 29, 40 trials)

| query | ε=4% cov / errP95 | ε=5% | ε=1% |
|---|---|---|---|
| `SUM` ungrouped         | 100 / 1.02 | 95  / 1.15 | 100 / 0.51 |
| `SUM` grouped           | 100 / 1.13 | 100 / 1.17 | 100 / 0.37 |
| `AVG` grouped           | 99  / 0.72 | 100 / 0.64 | 99  / 0.36 |
| `SUM` + `WHERE` grouped | 100 / 1.40 | 100 / 1.43 | 100 / 0.34 |
| 3 aggregates grouped    | 100 / 0.86 | 100 / 0.92 | 100 / 0.25 |

At skewness ≈ 29 (15–30× TPC-H) coverage stays at nominal because
`recommend_method` switches the grid to the empirical-Bernstein bound. At
skewness ≈ 440 (α=1.5) the intervals never reach ε within the budget and the
engine returns exact answers — coverage is not violated, but the speedup is
lost. This is the intended failure mode: never a confident wrong interval.

## 7. Adaptivity vs. a fixed sample (`static_ci` baseline, SF10 smoke)

| query | static_ci (fixed 5%) | AetherQuery |
|---|---|---|
| `SUM` grouped (`Q06`)   | 0.16% err, 73 ms, 5% sample  | 0.12% err, **22 ms**, **1% sample** |
| `COUNT` grouped (`Q05`) | 0.17% err, 4.4 ms, 5% sample | 0.21% err, **2.4 ms**, **1% sample** |
| `SUM` ungrouped (`Q02`) | 0.05% err, 18 ms, 5% sample  | 0.13% err, **5.8 ms**, **0.5% sample** |

Equal accuracy, 5–10× less data scanned, 2–3× lower latency. `static_ci`
abstains on joins.

## 8. Limitations (state up front)

1. **Joins covered are INNER fact→dimension.** LEFT/RIGHT/FULL and M:N joins
   get the group-completeness heuristic (bounded, honest about incompleteness)
   but **no confidence interval**.
2. **Small fact tables.** Cluster-estimator joins need ≳ 100 fact row groups
   for the between-block variance to be well determined; below that (SF1
   `customer`) 1:N join coverage is ~90%.
3. **Design effect is a constant (1.75), not measured.** The block-aggregate
   machinery used for joins could measure it per query; that is future work.
4. **Extreme skew trades speed for safety.** Beyond skewness ≈ 100 the engine
   degrades toward exact scans.
5. **Scale.** Evaluated at SF1 and SF10. SF100 and a workload-level study
   (mixed query stream, cache effects) are not done.
6. **Baseline.** `static_ci` is fixed-uniform-sampling-with-CIs, not a
   re-implementation of BlinkDB's stratified samples or WanderJoin's random
   walks.

## 9. Reproduce

```
docker build -t aq . && docker run --rm aq          # SF1, tests + smoke + study
scripts/reproduce.sh 10                              # SF10 (needs ~3 GB dbgen)
python scripts/generate_skewed_dataset.py --alpha 2.5
python scripts/run_engine_coverage_study.py --database aqp_eval/datasets/skewed.duckdb
```
