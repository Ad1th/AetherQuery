"""
Pushed-down sufficient statistics for confidence-interval-based stopping.

`runtime_sampling` historically stopped when two consecutive samples agreed
within a threshold. Iteration-to-iteration stability is not accuracy: a
consistently biased estimator is perfectly stable. This module gives the
controller a real basis for stopping -- a confidence interval whose width it
can compare against the user's error target -- by fetching, in one query per
sample, exactly the sufficient statistics `backend.stats` needs:

    COUNT(*), COUNT(*) FILTER (pred), and -- FILTERed by the predicate so the
    sampling denominator survives -- SUM(x), SUM(x*x), SUM(x*x*x), VAR_SAMP(x),
    skewness(x), MIN(x), MAX(x)      per GROUP BY group

plus the sampled relation's true row count (cached), so the *realized*
sampling fraction n/N is used rather than the nominal TABLESAMPLE percentage.
A constant design-effect inflation (SYSTEM_SAMPLING_DESIGN_EFFECT) widens
SUM/AVG intervals to account for TABLESAMPLE SYSTEM being row-group, not row,
sampling.

For an INNER fact->dimension join we can still form an honest interval by
treating the physical row group of the sampled (fact) table as the sampling
unit: `evaluate_join_sample_accuracy` groups the joined result by
`fact.rowid // 2048`, builds `backend.stats.design_effect.ClusterSampleStats`
per output group, and calls `estimate_clustered`. The between-block variance
absorbs the 1:N fan-out that makes a naive 1/f expansion under-cover. Other
join shapes (LEFT/RIGHT/FULL, non-equi) keep `run_runtime_sampling`'s
group-completeness heuristic.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import replace
from typing import Any

from backend.core.executor import _execute_source_query
from backend.core.parser import ParsedQuery
from backend.stats import (
    Aggregate,
    AggregateCell,
    Correction,
    EstimateSet,
    Method,
    SampleStats,
    adjust_coverage_level,
    estimate_query,
    recommend_method,
)
from backend.stats.design_effect import (
    DUCKDB_VECTOR_SIZE,
    BlockAggregate,
    ClusterSampleStats,
    estimate_clustered,
    estimate_design_effect,
)

_AGG_BY_FUNC = {
    "count": Aggregate.COUNT,
    "sum": Aggregate.SUM,
    "avg": Aggregate.AVG,
}

# Engine-native TABLESAMPLE SYSTEM draws whole row groups, not independent
# rows, so its SUM/AVG variance is larger than simple-random-sampling theory
# predicts. This constant inflates the interval variance for it. It also
# absorbs the CLT's mild small-n optimism, which is why a pure per-query DEFF
# measurement (`_measure_system_design_effect`, kept as a validation
# diagnostic) empirically covers slightly worse *and* costs an extra scan.
# On TPC-H the measured DEFF for grouped SUM is ~1.9-2.0, close to this value.
# COUNT is left at 1.0: it estimates a bounded indicator mean and the coverage
# study found it robust to the design.
SYSTEM_SAMPLING_DESIGN_EFFECT = 1.75
# Clamp for the measurement diagnostic: never below 1.0 (SRS beating cluster
# sampling is estimation noise), never above this (a runaway estimate).
MAX_MEASURED_DESIGN_EFFECT = 12.0

# SELECT COUNT(*) FROM <table> is answered from metadata in DuckDB; cache it
# per (source, table) so the interval path costs one extra query per process,
# not one per sample.
_POPULATION_CACHE: dict[tuple[str, str], int] = {}


def _population_size(table: str, source: str) -> int | None:
    key = (source, table)
    if key not in _POPULATION_CACHE:
        try:
            payload = _execute_source_query(f"SELECT COUNT(*) FROM {table}", source)
            _POPULATION_CACHE[key] = int(payload["rows"][0][0])
        except Exception:
            return None
    return _POPULATION_CACHE[key]


def _tablesample_from(table: str, source: str, sample_fraction: float) -> str:
    percent = sample_fraction * 100.0
    if source == "duckdb":
        return f"{table} TABLESAMPLE SYSTEM ({percent:.4f} PERCENT)"
    if source == "postgres":
        return f"{table} TABLESAMPLE SYSTEM ({percent:.4f})"
    # mysql: no TABLESAMPLE; caller handles the RAND() predicate
    return table


def _col_expr(expression: str) -> str:
    return f"CAST(({expression}) AS DOUBLE)"


def join_ci_is_defensible(parsed: ParsedQuery) -> bool:
    """
    True when expanding a one-sided (fact-table) sample by 1/f gives an
    approximately unbiased domain total, so the single-table estimators apply:

      * every join is INNER, and
      * the join is a fact -> dimension shape (N:1), which we approximate by
        requiring at least one equi-join predicate per join.

    M:N joins and outer joins break the "each fact row included independently
    with probability f" model and are left to the group-completeness heuristic.
    """
    if not parsed.joins:
        return False
    for join in parsed.joins:
        if join.join_type != "INNER":
            return False
        if "=" not in join.on_condition:
            return False
    return True


_SQL_KEYWORDS_AFTER_TABLE = {
    "join", "inner", "left", "right", "full", "cross", "natural", "on", "where",
    "group", "order", "limit", "using",
}


def _fact_alias(parsed: ParsedQuery) -> str:
    """The alias (or bare name) of the first table in FROM -- the sampled side."""
    from_match = re.search(
        r"(?is)\bFROM\s+([a-zA-Z_][a-zA-Z0-9_]*)(?:\s+(?:AS\s+)?([a-zA-Z_][a-zA-Z0-9_]*))?",
        parsed.raw_sql,
    )
    if not from_match:
        return parsed.table
    table, alias = from_match.group(1), from_match.group(2)
    if alias and alias.lower() not in _SQL_KEYWORDS_AFTER_TABLE:
        return alias
    return table


def _join_from_clause(parsed: ParsedQuery, source: str, sample_fraction: float) -> str:
    """
    The original FROM..JOIN..ON text with TABLESAMPLE injected after the primary
    (fact) table only. Mirrors join_sampling.build_stratified_join_query so the
    sampled join result is a one-sided sample of the fact table.
    """
    original_sql = parsed.raw_sql
    from_match = re.search(
        r"(?is)\bFROM\s+(.+?)(?:\s+WHERE|\s+GROUP\s+BY|\s+ORDER\s+BY|\s+LIMIT|$)",
        original_sql,
    )
    if not from_match:
        raise ValueError("could not extract FROM clause for join sufficient-stats")
    from_text = from_match.group(1).strip()

    percent = sample_fraction * 100.0
    if source == "duckdb":
        inject = lambda m: f"{m.group(1)} {m.group(2) or ''} TABLESAMPLE SYSTEM ({percent:.4f} PERCENT)"
    elif source == "postgres":
        inject = lambda m: f"{m.group(1)} {m.group(2) or ''} TABLESAMPLE SYSTEM ({percent:.4f})"
    else:
        return from_text  # mysql: RAND() predicate added by caller
    return re.sub(
        r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)(?:\s+([a-zA-Z_][a-zA-Z0-9_]*))?",
        inject,
        from_text,
        count=1,
    )


def build_sufficient_stats_sql(
    parsed: ParsedQuery, source: str, sample_fraction: float
) -> str:
    """
    One grouped query returning, per group, the pre-predicate row count and the
    sufficient statistics for every aggregate. The predicate is applied with
    FILTER rather than WHERE so `n_sample` (rows in the sample before WHERE and
    grouping) is recoverable as the sum of the raw per-group counts.
    """
    where = parsed.where_clause
    filt = f" FILTER (WHERE {where})" if where else ""

    # Alias each GROUP BY expression to a stable name: engines drop the table
    # qualifier from "l.l_shipmode" in the output column name, so reading it
    # back by the original text fails and every row collapses to one NULL group.
    select_parts: list[str] = [
        f"{expr} AS __aqp_grp_{i}" for i, expr in enumerate(parsed.group_by)
    ]
    select_parts.append("COUNT(*) AS __aqp_n_bucket")
    select_parts.append(f"COUNT(*){filt} AS __aqp_n_domain")

    for agg in parsed.aggregates:
        alias = agg.alias
        if agg.is_count_star:
            # COUNT(*) needs only the domain count, already captured above.
            continue
        col = _col_expr(agg.expression)
        select_parts.append(f"SUM({col}){filt} AS __aqp_sum__{alias}")
        select_parts.append(f"SUM({col} * {col}){filt} AS __aqp_sumxx__{alias}")
        select_parts.append(f"SUM({col} * {col} * {col}){filt} AS __aqp_sumxxx__{alias}")
        select_parts.append(f"VAR_SAMP({col}){filt} AS __aqp_var__{alias}")
        select_parts.append(f"MIN({col}){filt} AS __aqp_min__{alias}")
        select_parts.append(f"MAX({col}){filt} AS __aqp_max__{alias}")
        if source == "duckdb":
            select_parts.append(f"skewness({col}){filt} AS __aqp_skew__{alias}")

    if parsed.has_joins:
        from_clause = _join_from_clause(parsed, source, sample_fraction)
    else:
        from_clause = _tablesample_from(parsed.table, source, sample_fraction)
    sql = f"SELECT {', '.join(select_parts)} FROM {from_clause}"

    if source == "mysql":
        sql += f" WHERE (RAND() < {sample_fraction:.8f})"

    if parsed.group_by:
        sql += f" GROUP BY {', '.join(parsed.group_by)}"

    return sql


def _measure_system_design_effect(
    parsed: ParsedQuery, source: str, sample_fraction: float
) -> float | None:
    """
    Measure the TABLESAMPLE SYSTEM design effect for this query's SUM columns,
    from block-level aggregates over the same sample: DEFF = Var_cluster /
    Var_srs on the same rows, treating the fact row group as the sampling unit.

    Returns the worst (largest) DEFF across SUM cells, clamped to
    [1.0, MAX_MEASURED_DESIGN_EFFECT], or None if it cannot be measured
    (non-DuckDB, < 2 blocks, degenerate variance, no SUM/AVG column).
    """
    if source != "duckdb":
        return None
    sum_aggs = [a for a in parsed.aggregates if a.func.lower() in ("sum", "avg")]
    if not sum_aggs:
        return None
    population = _population_size(parsed.table, source)
    if population is None:
        return None

    where = parsed.where_clause
    filt = f" FILTER (WHERE {where})" if where else ""
    sel = [f"(rowid // {DUCKDB_VECTOR_SIZE}) AS __blk"]
    sel += [f"{g} AS __g{i}" for i, g in enumerate(parsed.group_by)]
    sel.append("COUNT(*) AS __n")
    sel.append(f"COUNT(*){filt} AS __nd")
    for a in sum_aggs:
        c = _col_expr(a.expression)
        sel.append(f"SUM({c}){filt} AS __s_{a.alias}")
        sel.append(f"SUM({c} * {c}){filt} AS __ss_{a.alias}")
    grp = ["__blk"] + [f"__g{i}" for i in range(len(parsed.group_by))]
    sql = (
        f"SELECT {', '.join(sel)} FROM {parsed.table} "
        f"TABLESAMPLE SYSTEM ({sample_fraction * 100.0:.4f} PERCENT) "
        f"GROUP BY {', '.join(grp)}"
    )
    try:
        payload = _execute_source_query(sql, source)
        cols = payload.get("columns", [])
        rows = [dict(zip(cols, r)) for r in payload.get("rows", [])]
        if not rows or "__blk" not in cols:
            return None
        return _design_effect_from_block_rows(parsed, rows, sum_aggs, population)
    except Exception:
        return None


def _design_effect_from_block_rows(parsed, rows, sum_aggs, population):
    total_blocks = max(1, math.ceil(population / DUCKDB_VECTOR_SIZE))
    ngrp = len(parsed.group_by)
    block_ids = sorted({r["__blk"] for r in rows})
    by_group: dict[tuple, dict[Any, dict[str, float]]] = {}
    for r in rows:
        gk = tuple(r.get(f"__g{i}") for i in range(ngrp))
        by_group.setdefault(gk, {})[r["__blk"]] = r
    if len(block_ids) < 2:
        return None

    worst: float | None = None
    for gk, per_block in by_group.items():
        for a in sum_aggs:
            blocks_list = []
            for blk in block_ids:
                r = per_block.get(blk)
                if r is None:
                    blocks_list.append(BlockAggregate(block_id=blk, n_rows=0, n_domain=0))
                else:
                    blocks_list.append(BlockAggregate(
                        block_id=blk,
                        n_rows=int(r.get("__n") or 0),
                        n_domain=int(r.get("__nd") or 0),
                        sum_x=float(r.get(f"__s_{a.alias}") or 0.0),
                        sum_xx=float(r.get(f"__ss_{a.alias}") or 0.0),
                    ))
            cluster = ClusterSampleStats(
                blocks=tuple(blocks_list),
                total_blocks=total_blocks,
                population_size=population,
            )
            deff = estimate_design_effect(Aggregate.SUM, cluster)
            if deff is not None and math.isfinite(deff):
                worst = deff if worst is None else max(worst, deff)

    if worst is None:
        return None
    return max(1.0, min(MAX_MEASURED_DESIGN_EFFECT, worst))


def fetch_sufficient_stats(
    parsed: ParsedQuery, source: str, sample_fraction: float
) -> tuple[dict[Any, dict[str, SampleStats]], int, str, float]:
    """
    Returns (cells_by_group, n_sample, sql, query_time_seconds) where
    cells_by_group maps a group key (tuple, or () when ungrouped) to
    {alias: SampleStats}.
    """
    sql = build_sufficient_stats_sql(parsed, source, sample_fraction)
    start = time.perf_counter()
    payload = _execute_source_query(sql, source)
    query_time = float(payload.get("time", time.perf_counter() - start))

    columns = payload.get("columns", [])
    rows = payload.get("rows", [])

    n_sample = sum(
        int(dict(zip(columns, r)).get("__aqp_n_bucket") or 0) for r in rows
    )
    n_sample = max(n_sample, 1)

    ungrouped = not parsed.group_by
    no_predicate = not parsed.where_clause
    common_kwargs: dict[str, Any] = {}
    if parsed.has_joins:
        # The sampling unit is a row of the sampled join result and its
        # population is the full join, whose size we do not compute. Expand by
        # the nominal fraction; SYSTEM_SAMPLING_DESIGN_EFFECT covers the extra
        # variance from 1:N fan-out (cluster sampling on the fact table).
        common_kwargs["nominal_fraction"] = max(1e-6, min(1.0, sample_fraction))
    else:
        population = _population_size(parsed.table, source)
        if population is not None:
            n_sample = min(n_sample, population)
            common_kwargs["population_size"] = population
        else:
            common_kwargs["nominal_fraction"] = max(1e-6, min(1.0, sample_fraction))

    cells_by_group: dict[Any, dict[str, SampleStats]] = {}
    for row_values in rows:
        row = dict(zip(columns, row_values))
        n_domain = int(row.get("__aqp_n_domain") or 0)
        group_key = (
            tuple(row.get(f"__aqp_grp_{i}") for i in range(len(parsed.group_by)))
            if parsed.group_by
            else ()
        )
        by_alias: dict[str, SampleStats] = {}

        for agg in parsed.aggregates:
            alias = agg.alias
            if agg.is_count_star:
                by_alias[alias] = SampleStats(
                    n_sample=n_sample,
                    n_domain=n_domain,
                    domain_is_universe=(ungrouped and no_predicate),
                    **common_kwargs,
                )
                continue

            sum_x = row.get(f"__aqp_sum__{alias}")
            sum_xx = row.get(f"__aqp_sumxx__{alias}")
            sum_xxx = row.get(f"__aqp_sumxxx__{alias}")
            var_x = row.get(f"__aqp_var__{alias}")
            min_x = row.get(f"__aqp_min__{alias}")
            max_x = row.get(f"__aqp_max__{alias}")
            skew_x = row.get(f"__aqp_skew__{alias}")
            by_alias[alias] = SampleStats(
                n_sample=n_sample,
                n_domain=n_domain,
                sum_x=float(sum_x) if sum_x is not None else 0.0,
                sum_xx=float(sum_xx) if sum_xx is not None else 0.0,
                sum_xxx=float(sum_xxx) if sum_xxx is not None else None,
                min_x=float(min_x) if min_x is not None else None,
                max_x=float(max_x) if max_x is not None else None,
                variance_direct=float(var_x) if var_x is not None else None,
                skewness_direct=float(skew_x) if skew_x is not None else None,
                **common_kwargs,
            )

        # Drop the spurious all-zero groups the FILTER trick can surface when a
        # GROUP BY value exists only among predicate-failing rows.
        if n_domain == 0 and parsed.where_clause and parsed.group_by:
            continue
        cells_by_group[group_key] = by_alias

    return cells_by_group, n_sample, sql, query_time


def evaluate_sample_accuracy(
    parsed: ParsedQuery,
    source: str,
    sample_fraction: float,
    *,
    coverage_level: float = 0.95,
    target_relative_error: float = 0.05,
    design_effect: float = SYSTEM_SAMPLING_DESIGN_EFFECT,
    multiplicity_correction: bool = True,
) -> tuple[EstimateSet, bool, dict[str, Any]]:
    """
    Take one sample, form a family of confidence intervals over the result grid,
    and report whether every cell's relative half-width is within target.

    `design_effect` inflates SUM/AVG interval variance. The default constant is
    used on the hot path (measuring it per query needs a second block-grouped
    scan and, empirically, the constant covers both the block design effect and
    the CLT's small-n optimism better than a pure DEFF measurement does).
    `_measure_system_design_effect` is available as a validation diagnostic --
    on TPC-H it returns ~1.9-2.0 for grouped SUM, close to the constant.

    The returned dict carries the point estimates and per-cell intervals in the
    shape `runtime_sampling` needs for its result payload.
    """
    cells_by_group, n_sample, sql, query_time = fetch_sufficient_stats(
        parsed, source, sample_fraction
    )

    agg_cells: list[AggregateCell] = []
    methods_seen: set[Method] = set()
    for group_key, by_alias in cells_by_group.items():
        for agg in parsed.aggregates:
            stats = by_alias.get(agg.alias)
            if stats is None:
                continue
            aggregate = _AGG_BY_FUNC.get(agg.func.lower(), Aggregate.SUM)
            # Inflate the design effect for SUM/AVG under block sampling; leave
            # COUNT alone.
            if aggregate is not Aggregate.COUNT and design_effect != stats.design_effect:
                stats = replace(stats, design_effect=design_effect)
            methods_seen.add(recommend_method(stats, aggregate).method)
            agg_cells.append(
                AggregateCell(
                    alias=agg.alias,
                    aggregate=aggregate,
                    stats=stats,
                    group_key=group_key if parsed.group_by else None,
                )
            )

    # estimate_query applies one interval method across the whole grid. If any
    # cell is skewed enough that recommend_method wants a finite-sample bound,
    # use it for all of them rather than under-covering the skewed one.
    grid_method = (
        Method.EMPIRICAL_BERNSTEIN
        if Method.EMPIRICAL_BERNSTEIN in methods_seen
        else Method.CLT
    )
    estimate_set = estimate_query(
        agg_cells,
        coverage_level=coverage_level,
        method=grid_method,
        correction=(
            Correction.BONFERRONI
            if (multiplicity_correction and len(agg_cells) > 1)
            else Correction.NONE
        ),
    )
    met = estimate_set.meets_target(target_relative_error)

    # Build the point-estimate result map / rows in runtime_sampling's shape.
    columns = list(parsed.group_by) + [a.alias for a in parsed.aggregates]
    result_map: dict[str, Any] = {}
    for group_key, by_alias_est in estimate_set.estimates.items():
        row: dict[str, Any] = {}
        if parsed.group_by and isinstance(group_key, tuple):
            for col, val in zip(parsed.group_by, group_key):
                row[col] = val
        for agg in parsed.aggregates:
            est = by_alias_est.get(agg.alias)
            row[agg.alias] = est.estimate if est is not None else None
        key = str(tuple(row.get(c) for c in parsed.group_by)) if parsed.group_by else "row_0"
        result_map[key] = row

    rows = [[entry.get(col) for col in columns] for entry in result_map.values()]

    detail = {
        "columns": columns,
        "rows": rows,
        "result_map": result_map,
        "n_sample": n_sample,
        "sample_query": sql,
        "query_time": query_time,
        "max_relative_half_width": estimate_set.max_relative_half_width,
        "unresolved_cells": len(estimate_set.unresolved),
        "design_effect": design_effect,
        "design_effect_measured": False,
        "ci": estimate_set.to_dict(),
    }
    return estimate_set, bool(met), detail


def build_join_block_stats_sql(
    parsed: ParsedQuery, source: str, sample_fraction: float
) -> str:
    """
    Sampled join, grouped by the fact table's physical row group as well as the
    query's GROUP BY, returning per (block, group): row count, in-domain count,
    and SUM / SUM(x^2) of each aggregate's column.
    """
    fact = _fact_alias(parsed)
    where = parsed.where_clause
    filt = f" FILTER (WHERE {where})" if where else ""

    select_parts: list[str] = [f"({fact}.rowid // {DUCKDB_VECTOR_SIZE}) AS __aqp_blk"]
    select_parts += [
        f"{expr} AS __aqp_grp_{i}" for i, expr in enumerate(parsed.group_by)
    ]
    select_parts.append("COUNT(*) AS __aqp_n_rows")
    select_parts.append(f"COUNT(*){filt} AS __aqp_n_domain")
    for agg in parsed.aggregates:
        if agg.is_count_star:
            continue
        col = _col_expr(agg.expression)
        select_parts.append(f"SUM({col}){filt} AS __aqp_sum__{agg.alias}")
        select_parts.append(f"SUM({col} * {col}){filt} AS __aqp_sumxx__{agg.alias}")

    sql = (
        f"SELECT {', '.join(select_parts)} "
        f"FROM {_join_from_clause(parsed, source, sample_fraction)}"
    )
    if source == "mysql":
        sql += f" WHERE (RAND() < {sample_fraction:.8f})"
    group_keys = ["__aqp_blk"] + [f"__aqp_grp_{i}" for i in range(len(parsed.group_by))]
    sql += f" GROUP BY {', '.join(group_keys)}"
    return sql


def evaluate_join_sample_accuracy(
    parsed: ParsedQuery,
    source: str,
    sample_fraction: float,
    *,
    coverage_level: float = 0.95,
    target_relative_error: float = 0.05,
    multiplicity_correction: bool = True,
) -> tuple[EstimateSet, bool, dict[str, Any]]:
    """
    Confidence intervals for an INNER fact->dimension join, using the fact
    table's physical row group as the sampling unit (cluster sampling). The
    between-block variance absorbs the 1:N fan-out; degrees of freedom come
    from the block count, not the row count, so these intervals are wide and
    honest where a 1/f expansion with SRS variance would badly under-cover.

    Requires DuckDB (needs `rowid` and SYSTEM block sampling).
    """
    fact_pop = _population_size(parsed.table, source)
    if fact_pop is None or source != "duckdb":
        # Fall back to the single-table path's shape; caller keeps the heuristic.
        return evaluate_sample_accuracy(
            parsed, source, sample_fraction,
            coverage_level=coverage_level,
            target_relative_error=target_relative_error,
            multiplicity_correction=multiplicity_correction,
        )

    _corr = Correction.BONFERRONI if multiplicity_correction else Correction.NONE
    total_blocks = max(1, math.ceil(fact_pop / DUCKDB_VECTOR_SIZE))
    sql = build_join_block_stats_sql(parsed, source, sample_fraction)
    start = time.perf_counter()
    try:
        payload = _execute_source_query(sql, source)
    except Exception:
        # rowid is unavailable on views, or the SQL did not bind. Degrade to the
        # naive-expansion path rather than failing the query; it under-covers on
        # 1:N joins but the caller's completeness heuristic still applies.
        return evaluate_sample_accuracy(
            parsed, source, sample_fraction,
            coverage_level=coverage_level,
            target_relative_error=target_relative_error,
            multiplicity_correction=multiplicity_correction,
        )
    query_time = float(payload.get("time", time.perf_counter() - start))
    cols = payload.get("columns", [])
    raw = [dict(zip(cols, r)) for r in payload.get("rows", [])]

    if not raw:
        # Empty sample: no interval, keep sampling.
        empty = EstimateSet(
            estimates={}, per_interval_coverage=coverage_level,
            family_wise_coverage=coverage_level, correction=_corr,
            num_intervals=0, notes=("join sample returned no rows",),
        )
        return empty, False, {
            "columns": list(parsed.group_by) + [a.alias for a in parsed.aggregates],
            "rows": [], "result_map": {}, "n_sample": 1, "blocks_sampled": 0,
            "sample_query": sql, "query_time": query_time,
            "max_relative_half_width": None, "unresolved_cells": 0,
            "ci": empty.to_dict(),
        }

    ngrp = len(parsed.group_by)

    # Per (block, group): the raw rows. A group that is absent from a sampled
    # block contributes a genuine zero in that block, and those zeros MUST be
    # included in the between-block variance -- otherwise a sparse group's
    # block_fraction is computed only over the blocks it happened to land in,
    # which over-states the sampling rate and inflates its estimate.
    per_block_group: dict[Any, dict[tuple, dict[str, float]]] = {}
    sampled_block_ids: set[Any] = set()
    n_sample = 0
    for row in raw:
        blk = row.get("__aqp_blk")
        sampled_block_ids.add(blk)
        n_sample += int(row.get("__aqp_n_rows") or 0)
        gk = tuple(row.get(f"__aqp_grp_{i}") for i in range(ngrp))
        rec = per_block_group.setdefault(blk, {}).setdefault(
            gk, {"n_rows": 0, "n_domain": 0}
        )
        rec["n_rows"] += int(row.get("__aqp_n_rows") or 0)
        rec["n_domain"] += int(row.get("__aqp_n_domain") or 0)
        for agg in parsed.aggregates:
            if agg.is_count_star:
                continue
            rec[f"sum_{agg.alias}"] = rec.get(f"sum_{agg.alias}", 0.0) + float(
                row.get(f"__aqp_sum__{agg.alias}") or 0.0
            )
            rec[f"sumxx_{agg.alias}"] = rec.get(f"sumxx_{agg.alias}", 0.0) + float(
                row.get(f"__aqp_sumxx__{agg.alias}") or 0.0
            )
    n_sample = max(n_sample, 1)
    all_blocks_sampled = max(1, len(sampled_block_ids))
    group_keys = {gk for bg in per_block_group.values() for gk in bg}

    block_fraction = max(1e-9, all_blocks_sampled / total_blocks)
    join_size_est = max(n_sample, round(n_sample / block_fraction))

    num_intervals = max(1, len(group_keys) * max(1, len(parsed.aggregates)))
    per_interval = adjust_coverage_level(coverage_level, num_intervals, _corr)

    # blocks[group_key][alias] -> BlockAggregate for EVERY sampled block (zeros
    # where the group is absent), so blocks_sampled is the same for every group.
    blocks: dict[tuple, dict[str, list[BlockAggregate]]] = {}
    for gk in group_keys:
        for agg in parsed.aggregates:
            lst: list[BlockAggregate] = []
            for blk in sampled_block_ids:
                rec = per_block_group.get(blk, {}).get(gk)
                if rec is None:
                    lst.append(BlockAggregate(block_id=blk, n_rows=0, n_domain=0))
                elif agg.is_count_star:
                    lst.append(BlockAggregate(
                        block_id=blk, n_rows=rec["n_rows"], n_domain=rec["n_domain"],
                    ))
                else:
                    lst.append(BlockAggregate(
                        block_id=blk, n_rows=rec["n_rows"], n_domain=rec["n_domain"],
                        sum_x=rec.get(f"sum_{agg.alias}", 0.0),
                        sum_xx=rec.get(f"sumxx_{agg.alias}", 0.0),
                    ))
            blocks.setdefault(gk, {})[agg.alias] = lst

    estimates: dict[Any, dict[str, Any]] = {}
    for gk, per_alias in blocks.items():
        key = gk if ngrp else None
        for agg in parsed.aggregates:
            aggregate = _AGG_BY_FUNC.get(agg.func.lower(), Aggregate.SUM)
            group_blocks = tuple(per_alias[agg.alias])
            group_rows = sum(b.n_rows for b in group_blocks)
            cluster = ClusterSampleStats(
                blocks=group_blocks,
                total_blocks=total_blocks,
                population_size=max(join_size_est, group_rows + 1),
            )
            estimates.setdefault(key, {})[agg.alias] = estimate_clustered(
                aggregate, cluster, coverage_level=per_interval
            )

    estimate_set = EstimateSet(
        estimates=estimates,
        per_interval_coverage=per_interval,
        family_wise_coverage=coverage_level,
        correction=_corr,
        num_intervals=num_intervals,
        notes=(
            "cluster estimator: the sampled fact table's row group is the "
            "sampling unit, so the interval rests on the block count",
        ),
    )
    met = estimate_set.meets_target(target_relative_error)
    # The between-block variance is itself only well determined with enough
    # blocks. Below ~20 sampled fact row groups the interval can look tight by
    # luck, so refuse to stop and let the controller escalate.
    MIN_BLOCKS_TO_STOP = 20
    if met and all_blocks_sampled < min(MIN_BLOCKS_TO_STOP, total_blocks):
        met = False

    columns = list(parsed.group_by) + [a.alias for a in parsed.aggregates]
    result_map: dict[str, Any] = {}
    for gk, by_alias_est in estimate_set.estimates.items():
        row: dict[str, Any] = {}
        if ngrp and isinstance(gk, tuple):
            for col, val in zip(parsed.group_by, gk):
                row[col] = val
        for agg in parsed.aggregates:
            est = by_alias_est.get(agg.alias)
            row[agg.alias] = est.estimate if est is not None else None
        key = str(tuple(row.get(c) for c in parsed.group_by)) if ngrp else "row_0"
        result_map[key] = row
    rows = [[entry.get(col) for col in columns] for entry in result_map.values()]

    detail = {
        "columns": columns,
        "rows": rows,
        "result_map": result_map,
        "n_sample": n_sample,
        "blocks_sampled": all_blocks_sampled,
        "sample_query": sql,
        "query_time": query_time,
        "max_relative_half_width": estimate_set.max_relative_half_width,
        "unresolved_cells": len(estimate_set.unresolved),
        "ci": estimate_set.to_dict(),
    }
    return estimate_set, bool(met), detail
