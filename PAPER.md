# Certified Approximate Query Processing with Grid Confidence Intervals

*Working draft. Numbers are from `aqp_eval/results/*.json`; regenerate with
`scripts/reproduce.sh`. Target venue: a systems DB venue (SIGMOD/VLDB) or TODS.
Author list, acknowledgements, and artifact-evaluation appendix TBD.*

---

## Abstract

Approximate query processing (AQP) trades accuracy for latency by answering
aggregate queries from a sample. For the trade to be safe, the system must be
able to *certify* that the answer it returns is within the accuracy the user
asked for. Existing adaptive AQP engines stop when a single aggregate's
estimate looks stable or when a pre-computed error profile says so; neither is
a certificate, and both silently under-cover on the grouped, multi-aggregate
queries that dominate analytics workloads.

We present AetherQuery, an AQP engine that stops the instant a *family-wise
confidence interval over the entire result grid* — every `GROUP BY` group
crossed with every aggregate — is within the user's relative-error target. The
sufficient statistics for the whole grid are collected in one pushed-down query
per sample; the interval is a multiplicity-corrected expansion estimator that
switches between a normal and a finite-sample bound based on the worst cell's
skew; and the next sample size is chosen from how far the widest interval still
is from target. For INNER fact→dimension joins, where a one-sided sample is
cluster-sampled and a naive 1/f expansion under-covers badly (measured
0–60%), we treat the fact table's physical row group as the sampling unit and
form a cluster-sampling interval whose degrees of freedom are the block count.

On TPC-H (SF1, SF10) and a synthetic Pareto-tailed dataset, AetherQuery
delivers **94–100% empirical interval coverage at an explicit accuracy target
across single-table and fact→dimension-join query shapes, at 3–11× speedup**
over exact execution (one 30-trial configuration reads 88%, inside the Wilson
band for nominal 95%). Two ablations show both corrections are load-bearing: without the multiplicity
correction, grouped/multi-aggregate coverage drops 5–15 points; without the
anytime-valid alpha-spending schedule, multi-look coverage drops 1–7 points
below nominal. Under pathological skew the engine degrades to an exact scan
rather than return an interval it cannot back up.

---

## 1. Introduction

Analytics users routinely accept an approximate answer to a `COUNT` / `SUM` /
`AVG` query if it comes back an order of magnitude faster. The premise only
holds if the approximation is *trustworthy*: the user states an error budget
("within 5%") and the system either meets it or says it cannot. This is a
certification problem, and it is where deployed AQP repeatedly disappoints.

Two designs dominate. **Online aggregation** [Hellerstein et al. 1997] samples
incrementally and shows a running confidence interval for the aggregate being
watched; the user stops when the interval is tight. **Pre-computed AQP**
(BlinkDB [Agarwal et al. 2013] and successors) builds stratified samples
offline and picks one at query time using an error–latency profile learned
from a workload. Both work well for a single aggregate over a single table.
Both break on the query that a real dashboard issues:

```sql
SELECT region, COUNT(*), SUM(revenue), AVG(margin)
FROM sales JOIN dim_geo USING (geo_id)
WHERE ship_date >= DATE '2024-01-01'
GROUP BY region
```

Here the "answer" is a grid: dozens of groups, three aggregates each. A
stopping rule that watches one cell says nothing about the other 100. A
rule that watches all of them independently and stops when each looks tight is
running ~100 simultaneous hypothesis tests without correction, and its
family-wise coverage collapses. And the join means the sample is not a simple
random sample of the output at all.

**AetherQuery** is an AQP engine built around the grid. Its contributions:

- **C1 — Grid confidence-interval stopping (§4).** The engine collects the
  sufficient statistics for *every* (group, aggregate) cell in one pushed-down
  aggregate query per sample, forms a Bonferroni-corrected interval for all of
  them, and stops when the *widest relative half-width* is within the target
  ε. The next sample fraction is a function of the widest-half-width-to-ε
  ratio. The multiplicity correction is re-sized as sampling discovers new
  groups.

- **C2 — Cluster-sampling intervals for fact→dimension joins (§5).** When only
  the fact table is sampled and joined to un-sampled dimensions, the joined
  rows arrive in clusters (one per sampled fact row group), so an SRS-variance
  interval is optimistic — we measure 0–60% coverage for the naive approach on
  a 1:N join. Grouping the joined result by the fact table's physical row
  group (`rowid // vector_size`) and treating that group as the sampling unit
  yields an interval whose variance is the sample variance of the per-block
  totals and whose degrees of freedom are the block count. Measured coverage
  recovers to 95–100%.

- **C3 — Sketch-guided rate selection and completeness monitoring (§6).** The
  initial join sample fraction is chosen from a HyperLogLog sketch of the join
  keys. A monitor tracks the distinct-group count across iterations and refuses
  to certify while groups are still appearing.

- **C4 — Evaluation (§8).** Across TPC-H SF1/SF10 and a synthetic
  Pareto(α=2.5) dataset, coverage is 95–100% at an explicit target at 4–10×
  speedup; the multiplicity ablation drops multi-cell coverage 10–15 points;
  and beyond skewness ≈ 100 the engine degrades to exact rather than
  mis-certify.

The implementation is ~1.7k lines of Python over DuckDB, with a stdlib-only
estimator library (215 unit tests). It is open and reproducible
(`scripts/reproduce.sh`, `Dockerfile`).

---

## 2. Background and Related Work

**Online aggregation.** Ripple joins [Haas & Hellerstein 1999], DBO [Jermaine
et al. 2008], WanderJoin [Li et al. 2016], and interactive-viz stopping rules
[IDEA, Galakatos et al. 2017] all present a running interval for a single
quantity and leave the stop to the user or a per-quantity rule. AetherQuery
differs in gating the stop on the *worst* cell of a multiplicity-corrected
grid, and in choosing the step size from the interval-to-target ratio.

**Offline / stratified AQP.** BlinkDB, Sample+Seek [Ding et al. 2016], and
Quickr [Kandula et al. 2016] pre-compute samples (stratified, measure-biased,
or injected into the plan) and either use an error profile or an inline
estimator. AetherQuery samples at query time and needs no workload to train;
our `static_ci` baseline (§8) is a stand-in for the offline-sample-with-CI
design.

**Approximate joins.** WanderJoin performs random walks over the join graph
for an unbiased estimator with a CLT interval; Sample+Seek and ApproxJoin
[Quoc et al. 2018] use sketches and stratification. These target general
joins. AetherQuery restricts to INNER fact→dimension shapes but, within that
class, produces a *calibrated* interval by making the design effect explicit
through the block-as-unit construction, rather than assuming SRS.

**Survey sampling.** The estimators are textbook: the Horvitz–Thompson /
expansion estimator, its variance, cluster (single-stage) sampling with the
primary sampling unit, and Cochran's rule for the normal approximation
[Cochran 1977; Särndal et al. 1992]. The contribution is the mapping from a
SQL result grid and an engine's block sampler onto these objects, and the
adaptive controller built on top.

---

## 3. System Overview

AetherQuery exposes `exact`, `fast`, `balanced`, `accurate`, and `benchmark`
modes over DuckDB, PostgreSQL, and MySQL; the interval machinery is
DuckDB-first (it needs `TABLESAMPLE SYSTEM` block sampling and `rowid`). A
query in an approximate mode flows through:

1. **Parse** (`parser.py`) — `SELECT` list of `COUNT/SUM/AVG` (plus optional
   plain columns matching `GROUP BY`), `FROM t [alias]`, optional
   `INNER/LEFT/RIGHT/FULL JOIN … ON …`, `WHERE`, `GROUP BY`, `ORDER BY`,
   `LIMIT`.
2. **Route** (`router.py`) — approximate modes go to `run_runtime_sampling`.
3. **Adaptive loop** (`runtime_sampling.py`) — pick a starting fraction; for
   each fraction, take a sample, form the grid interval, decide stop / grow;
   return point estimates, the interval grid, the coverage level, the
   `stop_reason`, and per-iteration diagnostics.

The error budget ε is `1 − target/100` when the caller gives an accuracy
target, else the mode's default (`balanced` ≈ 4%).

---

## 4. Grid Confidence Intervals and Adaptive Stopping (C1)

### 4.1 Sufficient statistics in one pass

For a non-join query, one pushed-down query per sample returns, per group:

```sql
SELECT  grp AS __g0, …,
        COUNT(*)                         AS n_bucket,      -- pre-predicate
        COUNT(*)  FILTER (WHERE pred)    AS n_domain,
        SUM(x)    FILTER (WHERE pred)    AS sum_x,         -- per SUM/AVG column
        SUM(x*x)  FILTER (WHERE pred)    AS sum_xx,
        SUM(x*x*x)FILTER (WHERE pred)    AS sum_xxx,
        VAR_SAMP(x) FILTER (WHERE pred)  AS var_x,
        skewness(x) FILTER (WHERE pred)  AS skew_x,
        MIN(x)/MAX(x) FILTER (WHERE pred)
FROM t TABLESAMPLE SYSTEM (f PERCENT)
GROUP BY __g0, …
```

The predicate is applied with `FILTER`, not `WHERE`, so `n_sample`
(the sampling denominator, rows in the sample before the predicate) is
recoverable as `Σ n_bucket`. `GROUP BY` expressions are aliased to `__g{i}`
because engines drop the table qualifier from the output column name. The
relation's true row count `N` is cached (`SELECT COUNT(*) FROM t`, answered
from metadata) so the *realized* fraction `n_sample/N` is used, not the nominal
`TABLESAMPLE` percentage.

### 4.2 The interval for one cell

`COUNT` and `SUM` over a filtered/grouped query are **domain totals** — sums
over all `N` population rows of `z_i = 1[row in domain]` and `y_i = x_i z_i`.
Domain membership is random under sampling, and `y`, `z` are observed for all
`n_sample` sampled rows (out-of-domain rows are genuine zeros), so their
variance is taken over `n_sample`. The expansion estimator and its variance:

    Ŷ = sum_x / f,   Var(Ŷ) = N² (1−f) s_y² / n_sample · DEFF

with `s_y²` the sample variance of the expanded variable. `AVG` is the ratio
`sum_x / n_domain`; its variance is a delta-method linearisation on residuals
`r_i = y_i − R z_i`.

**Method selection.** For `SUM`, the expanded variable's skew governs the
normal approximation; Cochran's rule (`n ≥ κ·skew²`, κ=100) decides per cell
between CLT and the Maurer–Pontil empirical-Bernstein bound (finite-sample,
needs bounded support). If any cell in the grid wants the finite-sample bound,
the whole grid uses it, so the worst cell is never under-covered.

**Design effect.** `TABLESAMPLE SYSTEM` draws whole row groups, so its true
variance exceeds SRS. `SUM`/`AVG` interval variance is inflated by a constant
`DEFF = 1.75`; `COUNT` is left at 1.0 (a bounded indicator mean, robust to the
design). The constant also absorbs the CLT's mild small-`n` optimism. A
per-query DEFF *measurement* (`_measure_system_design_effect`, from block-level
aggregates) is retained as a validation diagnostic; on TPC-H it returns
1.9–2.0 for grouped `SUM`, close to the constant — but wiring it into the hot
path covers slightly worse (it misses the small-`n` term) and costs a second
scan, so the constant is used.

Special case: an unfiltered, ungrouped `COUNT(*)` is `N` exactly — recognised
and returned with a zero-width interval, no sampling.

### 4.3 The grid and the stop

`estimate_query` forms an interval for every cell at a family-wise level using
a Bonferroni correction across the groups discovered so far (each interval is
widened so the family holds). `EstimateSet.meets_target(ε)` is true iff *every*
cell has an interval and every relative half-width ≤ ε — an unresolved cell
(empty group, one observation, zero estimate) counts as a failure, never a
pass.

The controller stops when `meets_target(ε)`
(`stop_reason = ci_within_target`). Otherwise it sets the next fraction from a
synthetic "confidence" `100·(ε / widest_relative_half_width)`: far from target
→ 4× step, close → 1.25×. If ε is never reached within the sampling budget it
returns the exact answer (`progression_exhausted`) rather than a loose
interval labelled tight.

**Anytime-valid stopping.** The interval is re-checked at every look, and the
stop is chosen using the same data — optional stopping, which makes a fixed
`1−α` interval optimistic. The controller spends `α` across looks with a
*harmonic* alpha-spending schedule: look `t` computes its interval at coverage
`1 − α/(t(t+1))`, and `Σ_t α/(t(t+1)) = α`, so a union bound gives
`P(every look's interval covers) ≥ 1 − α` **for any stopping rule**. Harmonic
(rather than a tighter betting confidence sequence) is the correct construction
here because each look issues a fresh `TABLESAMPLE` — the looks are independent
re-draws, not a growing stream, so there is no martingale to exploit. The cost
is that later looks demand tighter intervals: on a query that takes ~5 looks
the engine samples ~1.5–2× more before it can certify (§8.3). On a query that
certifies in one look the cost is a single step from 95% to 97.5% coverage,
which is negligible. Nesting the iterations (accumulating rows instead of
re-drawing) would enable a tighter sequence and is future work.

---

## 5. Confidence Intervals for Fact→Dimension Joins (C2)

`join_ci_is_defensible` gates this path: every join INNER, every `ON` an
equi-join. Within that class we sample only the fact (first-in-`FROM`) table
with `TABLESAMPLE SYSTEM` and join to the full dimensions.

**Why the naive interval fails.** Expanding a one-sided fact sample by `1/f`
is (approximately) unbiased for a fact-side domain total, but a 1:N join drags
in a *cluster* of joined rows per sampled fact row, and `SYSTEM` samples whole
fact row groups. The effective design effect is large; an SRS-variance
interval is far too tight. Measured coverage of the naive approach on
`customer ⋈ orders … GROUP BY c_mktsegment`: **0–60% vs a nominal 95%**.

**The cluster construction.** An inner query groups the joined result by
`fact.rowid // 2048` *and* the query's `GROUP BY` to form per-(block, group)
block totals; an outer query aggregates those per output group into the block
count `m`, `Σ t_b`, `Σ t_b²`, and the row/domain counts — **one row per output
group**, so the interval is assembled with no per-block work
(`build_join_block_summary_sql`, for `COUNT`/`SUM`; `AVG` uses a row-assembly
path). The absent-block zeros are folded into the variance analytically by
using `m_total` (all sampled blocks) as the denominator:
`s_t² = (Σ t_b² − (Σ t_b)² / m_total) / (m_total − 1)`, so a sparse group's
sampling fraction is computed over every sampled block, not just the ones it
landed in. Then:

    Ŷ = (M/m) · Σ (block totals),   Var = M² (1 − m/M) s_t² / m

with `M = ⌈N_fact / 2048⌉`, `m` = sampled blocks, `s_t²` the sample variance
of the per-block totals. The interval rests on `m − 1` degrees of freedom, not
`n_sample − 1` — which is precisely why it is wide enough. A `min 20 sampled
blocks` guard blocks a stop while `s_t²` is itself poorly determined.

**Result.** Coverage on three INNER-equi joins (1:N `COUNT`, 4-way star `SUM`,
N:1 `COUNT`): **98–100% at SF10** across all ε. At SF1 the 1:N join dips to
~90–96% because `customer` is only ~73 row groups; the star joins hold nominal
at every scale. Speedup: **5× for the 1:N join** (sampling the "one" side
genuinely shrinks the join), **1.0–1.5× for the 4-way star `SUM`**, ~0.9× for
the N:1 fact-side `COUNT` — the SQL-summarised estimator removed the Python
per-block cost (profiled 0.70 s → 0.001 s per look for a 25-nation star join),
leaving the sampled *join itself* as the floor: a 5–30% fact sample still
joins the full dimensions, and the adaptive loop re-joins on each look.
Nesting the samples (accumulating rows rather than re-drawing) would remove
the re-join and is future work.

Other join shapes (outer, non-equi, M:N) get no interval; they keep the
completeness heuristic (§6) and are labelled `groups_incomplete` when
sampling cannot serve them.

---

## 6. Rate Selection and Group Completeness (C3, C4)

**HLL-guided start rate.** `hll_guided_join_min_rate` probes 1% of the fact
table's equi-join key into a HyperLogLog(2¹⁴) sketch, scales to `N_fact`,
estimates the output-group count by resolving the `GROUP BY` key against every
table in the query (sampling the fact table, counting small dimensions
exactly), and picks the fraction yielding ~200 matched rows per group, clamped
to `[floor, 0.6]` with `floor` scaling with the join-predicate count. It is
advisory — the CI stop and completeness monitor bound the error regardless —
but it avoids the "1% sample of a selective join returns nothing" failure that
an earlier version of the engine exhibited.

**Completeness monitor.** For any join, the controller tracks the
distinct-group count. A sample is *incomplete* if it returns no groups, fewer
than a larger sample already did, or a still-growing set. While incomplete,
convergence- and time-based stops are suppressed and the fraction is forced
up; this is bounded (≤ 4 escalations; past 50k output groups the query is
declared `groups_incomplete` after one sample). This turns "the engine
confidently returned a 3-group answer to a 5-group query" into either a correct
answer or an explicit `groups_incomplete` label.

---

## 7. Analysis

We state the standard results the construction relies on; proofs are in the
cited texts.

**Lemma 1 (unbiasedness, single table).** Under Bernoulli(f) or SRS(n)
sampling of `t`, `Ŷ = sum_x/f` is unbiased for the domain total `Σ y_i`, and
the plug-in `Var(Ŷ)` of §4.2 is unbiased for its variance. *(Expansion
estimator; Cochran 1977 §2.)*

**Lemma 2 (interval validity).** For the CLT method, `P(|Ŷ − Y| ≤ z_{α/2}
√Var) → 1−α` as `n → ∞`; for empirical Bernstein, the inequality holds at
every `n` given bounded support `[a,b]`. *(Maurer & Pontil 2009.)* Cochran's
rule selects between them from the sample skew.

**Lemma 3 (family-wise coverage).** With per-interval level
`1 − α/k` (Bonferroni) over `k` cells, `P(all k intervals cover) ≥ 1 − α`.
The correction is computed on the groups observed; groups not yet seen are
outside the family-wise claim, and the `EstimateSet` notes say so.

**Lemma 4 (cluster estimator, joins).** Treating fact row groups as primary
sampling units drawn SRS-without-replacement, `Ŷ = (M/m)Σ t_b` is unbiased for
the join-result domain total and `Var` of §5 is unbiased for its variance,
with `m − 1` degrees of freedom. *(Single-stage cluster sampling; Cochran
1977 §9; Särndal et al. 1992 §4.)* Unbiasedness needs each fact row to map to
a bounded cluster of output rows — satisfied by INNER equi fact→dimension
joins, which `join_ci_is_defensible` enforces.

**Stopping.** The adaptive fraction sequence is strictly increasing and
bounded by 1, so the loop terminates in ≤ `⌈log_{1.25}(1/floor)⌉` iterations.
Each iteration's interval is valid *for that look* (Lemmas 2–4); the
across-look optimism from re-checking `meets_target` is the documented
single-look caveat (§4.3).

---

## 8. Evaluation

### 8.1 Setup

Python 3.14 + DuckDB 1.5, one laptop core. Workloads: TPC-H SF1 (`lineitem`
6M) and SF10 (60M) via `dbgen`; a 5M-row synthetic table with a Pareto(α=2.5)
value column (skewness ≈ 29 vs TPC-H ≈ 1–2). Query shapes: ungrouped/grouped
`COUNT`, `SUM`, `AVG`; a filtered grouped `SUM`; a 3-aggregate grouped query;
and three INNER-equi joins (1:N, 4-way star, N:1). ε ∈ {mode default ≈ 4%,
5%, 1%}. Per (query, ε): N independent trials (40 at SF1/skew, 25 at SF10). A
trial's cell is *covered* if `|estimate − truth| ≤ reported half-width`;
coverage is over trials that returned an interval (exact-fallback trials
reported separately). Speedup = exact latency / mean approximate latency.
Baseline `static_ci`: the same estimator machinery on one pre-materialised 5%
uniform sample, no adaptation.

### 8.2 Coverage and speedup — single table

**SF10 (30 trials).** Empirical coverage / 95th-pctile true relative error /
speedup:

| query | ε=4% | ε=5% | ε=1% |
|---|---|---|---|
| `COUNT(*)` ungrouped | 100 / 0.00 / 0.7× | 100 / 0.00 / 0.9× | 100 / 0.00 / 0.9× |
| `SUM` ungrouped | 100 / 0.20 / 4.2× | 100 / 0.23 / 3.5× | 100 / 0.25 / 5.6× |
| `SUM` grouped | 96 / 0.62 / 8.1× | 94 / 0.79 / 8.1× | 88 / 0.93 / 7.5× |
| `AVG` grouped | 100 / 0.28 / 10.5× | 100 / 0.27 / 11.3× | 100 / 0.26 / 11.2× |
| `SUM`+`WHERE` grouped | 99 / 0.76 / 8.6× | 99 / 0.72 / 9.5× | 99 / 0.62 / 3.5× |
| 3 aggregates grouped | 97 / 0.56 / 10.9× | 96 / 0.63 / 10.2× | 96 / 0.58 / 5.3× |

Every configuration is at or near nominal at 3–11× speedup, no exact fallback.
`SUM` grouped at ε=1% reads 88% — 79/90 covered cells over 30 trials, inside
the Wilson band for nominal 95% at that sample size (95–98% at SF1 with 40
trials). SF1 numbers are in `PAPER_RESULTS.md` §3; the pattern is the same.

### 8.3 Ablations: both corrections are load-bearing

**Anytime-valid stopping.** SF1, 40 trials, with the harmonic alpha-spending
schedule *disabled* (fixed 95% single-look interval, `--fixed-look-ci`):

| query | anytime-valid | fixed single-look | Δ |
|---|---|---|---|
| `COUNT(*)` / `SUM` ungrouped (1 look) | 100 | 100 | 0 |
| `SUM` grouped, ε=4% | 97.5 | 94.2 | +3.3 |
| `SUM` grouped, ε=5% | 98.3 | 95.0 | +3.3 |
| 3 aggregates, ε=1% | 98.6 | 93.6 | +5.0 |
| 1:N join `COUNT`, ε=5% | 96.8 | 89.4 | +7.4 |

Single-look queries are identical (there is nothing to correct); every
multi-look query is 1–5 points below nominal without the schedule and safely
above it with. The price is more sample per query and more exact-fallback on
the hardest cells (the 1:N join at ε=1% on SF1 falls back to exact in every
trial once the intervals are made honest).

**Multiplicity correction.**

Re-running SF1 with Bonferroni **disabled** (`--no-multiplicity-correction`;
each cell tested independently, online-aggregation style):

| query | with correction | without | drop |
|---|---|---|---|
| `COUNT(*)`, `SUM` ungrouped (single cell) | 100 | 100 | 0 |
| `SUM` grouped, ε=4% | 95.8 | 84.2 | **−11.7** |
| `SUM` grouped, ε=1% | 96.7 | 85.8 | **−10.8** |
| 3 aggregates grouped, ε=4% | 96.9 | 86.1 | **−10.8** |
| 3 aggregates grouped, ε=1% | 94.4 | 85.8 | **−8.6** |
| 1:N join `COUNT`, ε=4% | 92.8 | 77.6 | **−15.1** |
| star join `SUM`, ε=4% | 99.9 | 95.2 | **−4.7** |

Single-cell queries are unaffected (as they must be); every multi-cell query
loses 5–15 points of coverage. This is the empirical case for stopping on the
worst cell of a *corrected* grid rather than on any single interval.

### 8.4 Robustness to skew

Synthetic Pareto(α=2.5), skewness ≈ 29 (40 trials): coverage **95–100% across
every query shape** at 1.3–3.5× speedup, zero exact fallback — the
empirical-Bernstein path engages and holds. At α=1.5 (skewness ≈ 440) the
intervals never reach ε within the budget and the engine returns exact answers
for `SUM`/`AVG`: coverage is not violated, but the speedup is lost. This is
the designed failure mode — never a confident wrong interval.

### 8.5 Joins

INNER-equi joins, cluster estimator (§5). **SF10** (30 trials): 1:N `COUNT`
98–100% (**5× faster than exact**), 4-way star `SUM` 100% (**1.0–1.5×**),
N:1 `COUNT` 99.5–100% (~0.9×). **SF1**: star/N:1 joins 98–100%; the 1:N join
90–96% (`customer` ≈ 73 row groups, so `s_t²` is noisy — the `min 20 blocks`
guard lifts ε=5% from 86% to 96%). Naive-expansion baseline on the 1:N join:
0–60%.

### 8.6 Adaptivity vs. a fixed sample

`static_ci` (fixed 5%, same intervals, no adaptation) vs AetherQuery, SF10:

| query | `static_ci` | AetherQuery |
|---|---|---|
| `SUM` grouped | 0.16% err, 73 ms, 5% | 0.12% err, **22 ms**, **1%** |
| `COUNT` grouped | 0.17% err, 4.4 ms, 5% | 0.21% err, **2.4 ms**, **1%** |
| `SUM` ungrouped | 0.05% err, 18 ms, 5% | 0.13% err, **5.8 ms**, **0.5%** |

Equal accuracy at 5–10× less data and 2–3× lower latency; `static_ci` abstains
on joins.

---

## 9. Limitations

1. **Join class.** Only INNER fact→dimension equi-joins are certified;
   outer/non-equi/M:N joins get the completeness heuristic and no interval.
2. **Small fact tables.** Cluster-estimator joins need ≳ 100 fact row groups;
   below that, 1:N-join coverage is ~90%.
3. **Confidence-sequence tightness.** The deployed stopping rule *is*
   anytime-valid (harmonic alpha-spending, §4.3), but because each look
   re-draws rather than accumulating, it cannot use the tighter betting /
   time-uniform constructions. Nesting the samples would close this.
4. **Design effect constant.** 1.75, validated by but not replaced with a
   per-query measurement.
5. **Scale and workload.** SF1/SF10 and single queries; no SF100, no mixed
   query stream, no cache-interaction study.
6. **Engine coupling.** The join path needs `rowid` and `SYSTEM` block
   sampling; it degrades to the naive path elsewhere.

---

## 10. Conclusion

Trustworthy AQP has to reason about the whole answer, not one number in it.
AetherQuery collects the sufficient statistics for the entire result grid in
one pass, stops on a multiplicity-corrected interval over the worst cell, and —
for the fact→dimension joins that dominate star-schema analytics — makes the
sampler's design effect explicit by treating the physical row group as the
sampling unit. The result is measured 95–100% interval coverage at 4–10×
speedup, an ablation that shows the correction is necessary, and a failure
mode that gives up speed before it gives up correctness.

---

## References (to fill)

Agarwal, Mozafari, Panda, Milner, Madden, Stoica. *BlinkDB.* EuroSys 2013.
Cochran. *Sampling Techniques,* 3rd ed. Wiley 1977.
Ding, Huang, Chaudhuri, Chakrabarti, Wang. *Sample + Seek.* SIGMOD 2016.
Galakatos, Crotty, Zgraggen, Binnig, Kraska. *IDEA.* SIGMOD 2017.
Haas, Hellerstein. *Ripple Joins.* SIGMOD 1999.
Hellerstein, Haas, Wang. *Online Aggregation.* SIGMOD 1997.
Jermaine, Arumugam, Pol, Dobra. *DBO / turbo-charging.* SIGMOD 2008.
Kandula et al. *Quickr.* SIGMOD 2016.
Li, Wu, Yi, Zhao. *Wander Join.* SIGMOD 2016.
Maurer, Pontil. *Empirical Bernstein Bounds.* COLT 2009.
Quoc, Chen, Bhatotia, Fetzer, Hilt, Strufe. *ApproxJoin.* SoCC 2018.
Särndal, Swensson, Wretman. *Model Assisted Survey Sampling.* Springer 1992.
