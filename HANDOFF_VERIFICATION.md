# Handoff Verification & Publication Readiness

**Date:** 2026-08-30
**Scope:** Implement the unfinished work listed in `HANDOFF.md`, then run the
full verification pass (tests, build, lint, benchmark regeneration) and assess
readiness for (1) a patent filing and (2) a Q1 journal paper.

> **Update (same day, second pass):** the two blocking gaps in §5 have been
> addressed in code. Real confidence-interval stopping is now wired into the
> engine (`backend/core/sufficient_stats.py`), and JOIN sample-rate selection
> is now driven by HyperLogLog sketches, not a fixed table. See **§7** for what
> changed and the new measured coverage numbers; §5 is kept as the original
> assessment for context. Test count is now **207**.

---

## 1. Code implemented this pass

### 1.1 `error_bounds.progressive_refinement_with_bounds` (was `NotImplementedError`)

`backend/core/error_bounds.py:347` previously raised
`NotImplementedError("Integration with runtime_sampling pending")`. It is now a
working, self-contained algorithm:

* Starts from the sample fraction predicted by the inverted Hoeffding bound for
  the target error, then climbs a geometric ladder.
* At each fraction it runs a pushed-down sampled aggregate that *also* returns
  the per-group sample size and, for `SUM`/`AVG`, the value range / dispersion
  needed to form an interval.
* Forms a Hoeffding interval for `COUNT`/`SUM` and a CLT interval for `AVG` for
  every aggregate in every group, and stops as soon as the widest relative-error
  bound is within target (`stop_reason="error_bound_met"`).
* **JOIN queries are explicitly rejected** with `ValueError` rather than
  returned wrong: scaling a one-sided join sample by `1/f` is not unbiased in
  general and join-key multiplicity adds variance these bounds do not model.

It is **not wired into the default `/api/execute` path**. It is an opt-in entry
point. Rationale below (§4.2) — the project already contains a more rigorous
statistics layer (`backend/stats/`) that is the intended replacement, and
silently swapping the live stopping rule was out of scope for a handoff.

Tests: `backend/tests/test_error_bounds.py` (13 cases — interval direction /
monotonicity / degenerate cases, early-stop, escalation, per-group reporting,
JOIN rejection).

### 1.2 Adaptive JOIN sampling — the "empty groups / slower than exact" issue

`HANDOFF.md` §"Known issues" and `APPROXIMATE_JOIN_RESULTS.md` recorded that 1 %
JOIN sampling "frequently returned no rows and could be slower than exact".
Root cause found and fixed in `backend/core/runtime_sampling.py`:

1. **The JOIN sample-rate floor was dead code.** The block that raised the
   minimum fraction to 5–10 % for joins (added in an earlier commit) ran
   *before* the complexity-preset block, which then overwrote
   `config["progression"]` wholesale with `[0.02, 0.05, …]`. Every join query is
   scored "complex", so the floor never took effect and joins started at 2 %.
   The floor is now applied **after** the presets.

2. **Missing GROUP BY groups no longer count as "converged".** A join sample
   that returns fewer groups than a larger sample already produced — or an empty
   result, or a still-growing group set — is flagged `groups_incomplete` and
   blocks the `converged` / `time_budget_exceeded` stops, forcing escalation.

3. **Bounded escalation.** A high-cardinality GROUP BY over a join (one row per
   customer) can only show every group at a near-full scan. Escalation is capped
   at 4 iterations; past 50 000 output groups the query is declared
   `groups_incomplete` after the first sample instead of grinding to 100 %.

4. `groups_returned` and `groups_incomplete` are now surfaced on every iteration
   and on the final payload so callers/harnesses can see coverage.

Tests: `backend/tests/test_runtime_join_guard.py` (4 cases — floor is not
clobbered, incomplete groups force escalation, high-cardinality bail-out,
stable complete result still converges).

### 1.3 Parser bug fixed (pre-existing, blocking)

`SELECT col, COUNT(*) FROM t GROUP BY col` (grouped, **no** WHERE, no table
alias) raised `ValueError: Invalid SQL syntax in WHERE/GROUP BY/…` because the
table-alias sub-pattern captured `GROUP` as the alias. This broke eval queries
Q05/Q06 for the `aetherquery` policy. Fixed with a keyword negative-lookahead in
`backend/core/parser.py`. (181 pre-existing tests still pass.)

### 1.4 Harness / script fixes

* `aqp_eval/runners/run.py` — added `--database` / `--output` / `--dataset-label`
  so SF1 and SF10 artifacts can be regenerated without editing constants.
* `scripts/generate_tpch_data.py` — added missing `import sys` (raised
  `NameError` on any invocation).

---

## 2. Verification results

| Check | Result |
|---|---|
| `pytest backend/tests aqp_eval/metrics/test_compare.py` | **197 passed** (181 pre-existing + 16 new), 18 s |
| `npm run build` (frontend) | **passes** — `tsc -b` + `vite build`, 479 modules |
| `npm run lint` (frontend) | **19 errors, all pre-existing** — 17× `no-explicit-any`, 1 unused var, 1 constant-truthiness, in `App.tsx` / `QueryPlan.tsx` / `planToFlow.ts` / `QueryCacheSidebar.tsx`. Not touched by this work. |
| `aqp_eval.runners.run` SF1 + SF10 | regenerated `aqp_eval/results/sf{1,10}_smoke.json` against **clean** TPC-H |
| `scripts/test_tpch_joins.py` | runs end-to-end on clean SF10; all 5 queries return non-empty results (previously all "⚠️ No rows") |

`pytest` was not in `.venv` and was installed. `.venv` runs Python 3.14.7.

---

## 3. The benchmark database was corrupt — regenerated

`datasets/aetherquery.duckdb` (the DB the backend, scripts, and prior results
all used) is **not valid TPC-H**:

* `lineitem` = 60.6 M rows (SF10) but `orders` = 150 K and `customer` = 15 K
  (SF0.1) — join fan-out ~100× too high.
* **600 572 duplicate `(l_orderkey, l_linenumber)` pairs** — the table was
  loaded more than once.
* `l_discount` ∈ [0.0, **10.0**] (standard: [0.00, 0.10]); `l_quantity` max
  **5000** (std 50); `l_tax` max **8.0** (std 0.08) — ~546 K rows carry values
  ×100 out of range. `SUM(l_extendedprice*(1-l_discount))` — the flagship
  "revenue" JOIN aggregate — is **negative and meaningless** on this data.

Every accuracy figure in `APPROXIMATE_JOIN_RESULTS.md` and the revenue-JOIN
narrative was measured against this. Clean `tpch_sf1.duckdb` and
`tpch_sf10.duckdb` were generated with DuckDB's `dbgen` into `aqp_eval/datasets/`
(git-ignored) and all numbers below are against those.

**Action for the team:** delete / replace `datasets/aetherquery.duckdb`; it is
the default path in `backend/db/duckdb.py`.

---

## 4. Accuracy & speedup on clean TPC-H

`aetherquery` = the adaptive engine, `mode=balanced`, no accuracy target.
Error is mean relative error vs exact over all group×aggregate cells.

### 4.1 Non-JOIN aggregates (`aqp_eval` smoke, error %)

| Query | SF10 aether | best baseline | SF1 aether | best baseline |
|---|---|---|---|---|
| Q01 `COUNT(*)` ungrouped | **6.52** | fixed_ladder 0.24 | 2.38 | fixed_fraction 1.70 |
| Q02 `SUM` ungrouped | **5.24** | online_agg 0.08 | **15.81** | geometric 2.78 |
| Q05 `COUNT` grouped | **5.23** | geometric 0.10 | 2.33 | geometric 0.90 |
| Q06 `SUM` grouped | 1.17 | fixed_ladder 0.58 | 2.20 | aether best |
| Q09 2-way JOIN grouped | **14.00** | fixed_ladder 2.38 | **22.99** | geometric 2.47 |

Variance probe (6 trials, clean SF10, `balanced`):

* Q01 `COUNT(*)`: error **0.03 % – 8.84 %** (mean 3.5, σ 3.0), always
  `stop_reason=single_pass` at a **single 1 % sample**.
* Q09 JOIN grouped: error **0.0 % – 7.9 %** (mean 3.6, σ 2.8), `sample_rate`
  ranging 20 %–100 % run to run; one earlier full run measured **19.5 %**.

### 4.2 Why this matters

The adaptive stop criterion is *iteration-to-iteration stability*
(`convergence_error < threshold`), not a confidence interval. Two consecutive
samples that happen to agree within 4 % trigger `converged` even when the
estimate is 14–20 % from truth (Q09 SF10). For `balanced` with no target, an
ungrouped query gets **one** 1 % `TABLESAMPLE SYSTEM` sample and stops —
`SYSTEM` is block sampling, so its true variance is much larger than a 1 %
row sample, which is why Q01 swings 0–9 %.

The project already recognises this: `backend/stats/` (added on `feat/
statistical-validity`) is a full estimator library with real (ε, δ) intervals,
a coverage-study harness, sequential/anytime-valid analysis, and design-effect
handling for `SYSTEM` sampling. **It is not yet wired into
`runtime_sampling.py`.** Doing that — replacing `estimate_confidence` and the
delta-threshold stop with `backend.stats.estimate_query(...).meets_target(ε)` —
is the single highest-value next step and is a prerequisite for any accuracy
claim in a paper.

### 4.3 JOIN latency (clean SF10, `scripts/test_tpch_joins.py`)

| Query | groups | exact | approx | speedup | stop_reason |
|---|---|---|---|---|---|
| Q3 revenue by segment (2-way) | 5 | 0.76 s | 0.54 s | **1.4×** | converged |
| Q5 revenue by nation (3-way) | 25 | 0.59 s | 0.78 s | 0.8× | converged |
| Q12 shipping modes (2-way) | 7 | 0.50 s | 0.11 s | **4.4×** | converged |
| Q10 customer returns (3-way) | 993 K | 3.1 s | ~2 s | ~1.5× | groups_incomplete |
| Q18 large orders (2-way) | 999 K | 4.0 s | ~2.5 s | ~1.6× | groups_incomplete |

Low-cardinality-GROUP-BY joins now return correct, non-empty results in 2–3
iterations (the old failure mode is gone). High-cardinality-GROUP-BY joins are
correctly labelled `groups_incomplete` and bail after one sample instead of
grinding to a full scan — but they are fundamentally not AQP-suitable
(≈1 output row per input row).

---

## 5. Readiness assessment

### 5.1 Patent filing — *plausible for a provisional, not yet for a strong non-provisional*

**In favour**

* The combination claimed in `error_bounds.py` / `join_sampling.py` — runtime
  adaptive stratified join sampling with a group-completeness guard, HLL-guided
  rate selection, push-down join aggregation, cross-engine sampling strategy —
  is a coherent, non-trivial system and is now actually implemented and runs.
* A provisional needs a credible written description and enablement, not a
  peer-review-grade evaluation. The code + this document + the clean benchmarks
  clear that bar.

**Gaps a patent examiner / litigator would probe**

* **Prior art overlap.** Adaptive/online aggregation (Hellerstein 1997),
  BlinkDB, WanderJoin / ripple joins, Sample+Seek, ApproxHadoop, Quickr,
  Pulse — most individual elements here are known. The novelty has to be framed
  as the *specific mechanism* (e.g. the group-completeness escalation rule, or
  HLL-cardinality-driven per-table rate selection for stratified joins). That
  framing is not yet written down crisply.
* **HLL / Bloom are implemented but unused in the live path.** Claims about
  "HyperLogLog-guided sample size selection" are currently aspirational — the
  join floor is a fixed table (5/7/10 %), not HLL-derived. Either wire HLL in or
  narrow the claims.
* **`progressive_refinement_with_bounds` is not in the product path** and
  excludes joins. A claim to "provable (ε, δ) accuracy for approximate joins"
  is not supported by the code.
* Recommend: file a **provisional now** to get the date, listing the broad
  system; use the 12-month window to wire HLL + `backend/stats` in and run the
  evaluation, then convert with claims that match what the system actually does.

### 5.2 Q1 journal paper (VLDB / SIGMOD / TODS) — *not yet; ~4–6 months of focused work*

**What exists**

* A real system with a UI, multi-engine execution, and an adaptive sampler.
* A genuinely rigorous statistics package (`backend/stats/`) with its own
  coverage studies — this is the strongest publishable asset and is close to
  paper-grade *on its own* for the non-join single-look estimation story.
* A clean, extensible evaluation harness with 5 baseline policies.

**Blocking gaps**

1. **The headline engine's accuracy is uncontrolled.** As shipped, `balanced`
   mode returns 5–23 % error on standard TPC-H aggregates and declares
   "converged" while doing so. No Q1 reviewer will accept a stopping rule based
   on iteration stability. Fix: wire `backend/stats` into `runtime_sampling`,
   then show measured coverage ≈ nominal.
2. **No theoretical contribution for joins.** The paper narrative is approximate
   *joins*, but there is no convergence proof, no sample-complexity result, and
   the one-sided-sample estimator is known to be biased for many join shapes.
   Either restrict the paper to single-table AQP (where `backend/stats` already
   delivers) or do the join theory.
3. **Evaluation is a 5-query smoke test at one target.** Q1 needs: multiple
   scale factors, error targets swept (1/5/10 %), accuracy *and* measured
   coverage, convergence curves, latency/speedup distributions over many trials,
   at least one real-world dataset, and a comparison against a real baseline
   (BlinkDB / WanderJoin), not just fixed-fraction ladders.
4. **`TABLESAMPLE SYSTEM` design effect is unmeasured in the live path.** Block
   sampling inflates variance (visible in the Q01 0–9 % swing);
   `backend/stats/design_effect.py` exists to handle this but is not connected.
5. Reproducibility artifact (Docker + the clean `dbgen` script + notebooks).

**Recommended path to a paper**

* **Near term (weeks):** wire `backend/stats` into `runtime_sampling`; replace
  the delta-threshold stop with `EstimateSet.meets_target`; re-run the harness
  and report coverage. This alone could carry a solid **single-table AQP**
  workshop/short paper.
* **Medium term (months):** either (a) do the join estimator theory and a full
  TPC-H + real-data evaluation for a full VLDB/SIGMOD submission, or (b) scope
  the paper to "an adaptive, coverage-validated AQP engine with a pragmatic
  join-completeness heuristic" and lean on the empirical coverage results.

---

## 6. Files changed

```
backend/core/error_bounds.py       progressive_refinement_with_bounds implemented
backend/core/runtime_sampling.py    JOIN floor fix + group-completeness guard
backend/core/parser.py              grouped-query-without-WHERE alias bug fix
aqp_eval/runners/run.py             --database/--output/--dataset-label flags
scripts/generate_tpch_data.py       import sys
backend/tests/test_error_bounds.py        new (13 cases)
backend/tests/test_runtime_join_guard.py  new (4 cases)
aqp_eval/results/sf1_smoke.json      regenerated on clean TPC-H SF1
aqp_eval/results/sf10_smoke.json     regenerated on clean TPC-H SF10
```

Clean `aqp_eval/datasets/tpch_sf{1,10}.duckdb` were generated locally and are
git-ignored; regenerate with
`python -c "import duckdb;c=duckdb.connect('aqp_eval/datasets/tpch_sf1.duckdb');c.execute('INSTALL tpch');c.execute('LOAD tpch');c.execute('CALL dbgen(sf=1)')"`.

---

## 7. Second pass — real confidence intervals + HLL-guided joins

### 7.1 CI-based stopping for non-JOIN aggregates (`sufficient_stats.py`)

`backend/core/sufficient_stats.py` (new) fetches, in **one pushed-down query per
sample**, exactly the sufficient statistics `backend.stats` needs — per group:
`COUNT(*)`, `COUNT(*) FILTER (pred)`, `SUM(x)`, `SUM(x*x)`, `SUM(x*x*x)`,
`VAR_SAMP(x)`, `skewness(x)`, `MIN(x)`, `MAX(x)` — plus the relation's true row
count (cached) so the **realized** fraction `n/N` is used, not the nominal
`TABLESAMPLE` percentage.

`runtime_sampling.run_runtime_sampling` now, for non-JOIN aggregate queries:

* builds a Bonferroni-corrected `EstimateSet` over the whole result grid via
  `backend.stats.estimate_query`;
* stops when `EstimateSet.meets_target(ε)` — every cell's **relative CI
  half-width** ≤ ε — with `stop_reason="ci_within_target"`;
* takes ε from `accuracy_target` (`1 − target/100`) or the mode's error budget
  (`balanced` → 4%);
* grows the next sample in proportion to how far the widest interval still is
  from ε;
* reports the interval grid (`ci`), the realized `estimated_error`
  (= widest relative half-width), and a genuine `confidence` (the coverage
  level) instead of the old iteration-stability pseudo-confidence.

The old delta-threshold `converged` rule and `single_pass` short-circuit are
**bypassed** for these queries. JOINs are untouched (see §7.2).

**Effect on the smoke queries (clean TPC-H SF10, `balanced`, aetherquery policy):**

| Query | error before (delta stop) | error after (CI stop) | stop_reason | speedup |
|---|---|---|---|---|
| Q01 `COUNT(*)` | 6.52 % | **0.00 %** (recognised as census) | ci_within_target | ~1× |
| Q02 `SUM` ungrouped | 5.24 % | **0.05 %** | ci_within_target | ~3× |
| Q05 `COUNT` grouped | 5.23 % | **0.11 %** | ci_within_target | ~20× |
| Q06 `SUM` grouped | 1.17 % | **0.57 %** | ci_within_target | ~11× |

### 7.2 HyperLogLog-guided JOIN sample rate (`join_sampling.hll_guided_join_min_rate`)

The fixed 5/7/10 %-by-join-count floor is now only a lower bound. On a JOIN
query the engine:

1. draws a 1 % probe of the primary table's equi-join key,
2. feeds it to a `HyperLogLog(2^14)` sketch and scales the estimate to the full
   table,
3. estimates the output-group count the same way,
4. solves for the sample fraction that yields ≈200 matched rows per group,
   clamped to `[floor, 0.60]`.

The chosen rate and its inputs are reported on the payload as
`join_rate_selection` (`method: "hll_guided"` with `est_key_cardinality`,
`est_groups`, `chosen_rate`; falls back to `method: "fixed_floor"` on any probe
failure). This makes the "HLL-guided sample size selection" claim real code
rather than aspiration — though `est_groups` is weak when the GROUP BY key is
not on the primary table and should be improved (probe the dimension table).

### 7.3 Measured coverage (`scripts/run_engine_coverage_study.py`, new)

End-to-end study: run the engine N times per (query, target), compare the
reported 95 % interval against the true answer. TPC-H SF1, 40 trials,
`aqp_eval/results/engine_coverage_study.json`:

| query | target | empirical coverage | true err p50 / p95 | speedup |
|---|---|---|---|---|
| count_star | any | 100 % | 0.00 / 0.00 | ~1× |
| sum_ungrouped | 95 % | 97 % | 0.16 / 0.65 % | ~2× |
| avg_grouped | 95 % | 100 % | 0.26 / 0.89 % | ~6× |
| multi_agg | 95 % | 96 % | 0.50 / 1.75 % | ~6× |
| sum_filtered | 95 % | 97 % | 0.62 / 2.3 % | ~3× |
| **sum_grouped** | 95 % | **~88 %** | 0.6 / 2.1 % | ~5× |

So: COUNT, AVG, filtered and multi-aggregate queries cover **at or near the
nominal 95 %**. A plain grouped `SUM` of a heavy-tailed column still
**under-covers at ~85–92 %** — the CLT interval is too tight for that skew and
the finite-sample (empirical-Bernstein) path is not engaging aggressively
enough for the expanded (zero-inflated) variable. This is now a *measured,
visible* gap rather than a silent 5–20 % error.

### 7.4 Revised readiness

**Patent — ready for a strong provisional, and a non-provisional is now in
reach.** The three "aspirational" objections in §5.1 are answered: CI-driven
adaptive stopping, HLL-guided rate selection, and the group-completeness
escalation rule are all implemented and exercised by tests. Remaining before a
non-provisional: (a) tighten the HLL group-count probe, (b) decide whether to
claim the JOIN estimator (still heuristic — see below), (c) a proper prior-art
section positioning the *specific* mechanisms against BlinkDB / WanderJoin /
online aggregation.

**Q1 paper — a single-table AQP paper is now within a few weeks of
submittable.** The engine produces validated intervals with measured coverage;
the coverage study is the experimental backbone. To submit:

1. Close the grouped-SUM coverage gap (force empirical-Bernstein on the
   expanded variable when its skew is high; re-run the study to show ≥94 %).
2. Sweep scale factors (SF1/10/100) and more query shapes; add convergence
   curves and latency distributions (the study script already emits the raw
   data).
3. One real-world dataset (NYC taxi / Wikipedia clickstream).
4. A real baseline (BlinkDB or WanderJoin), not just fixed-fraction ladders.
5. Reproducibility artifact (Docker + the `dbgen` snippet + the study script).

**JOINs still need their own theory for a full VLDB/SIGMOD submission.** The
one-sided-sample-scaled-by-1/f estimator is biased for many join shapes and
`backend.stats` deliberately excludes it. The completeness guard and HLL rate
selection make joins *usable and honest* (labelled `groups_incomplete` when
they are not AQP-suitable), but a join paper needs an unbiased estimator with a
multiplicity-aware variance and a convergence result. That is genuine research,
not an engineering task — scope the near-term paper to single-table AQP.

### 7.5 Files added / changed in this pass

```
backend/core/sufficient_stats.py            new  - pushed-down sufficient stats + estimate_query wiring
backend/core/runtime_sampling.py            CI-stop path for non-JOIN aggregates; HLL rate hook
backend/core/join_sampling.py               hll_guided_join_min_rate + _join_key_columns
scripts/run_engine_coverage_study.py        new  - end-to-end coverage / error / speedup study
backend/tests/test_ci_stop.py               new  (5)
backend/tests/test_hll_join_rate.py         new  (5)
aqp_eval/results/sf{1,10}_smoke.json        regenerated with CI stopping
aqp_eval/results/engine_coverage_study*.json  new  - coverage study output
```
