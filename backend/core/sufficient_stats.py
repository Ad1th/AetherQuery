"""
Pushed-down sufficient statistics for confidence-interval-based stopping.

`runtime_sampling` historically stopped when two consecutive samples agreed
within a threshold. Iteration-to-iteration stability is not accuracy: a
consistently biased estimator is perfectly stable. This module gives the
controller a real basis for stopping -- a confidence interval whose width it
can compare against the user's error target -- by fetching, in one query per
sample, exactly the sufficient statistics `backend.stats` needs:

    COUNT(*), COUNT(*) FILTER (pred), SUM(x), VAR_SAMP(x), skewness(x),
    MIN(x), MAX(x)      per GROUP BY group

plus the sampled relation's true row count (cached), so the *realized*
sampling fraction n/N is used rather than the nominal TABLESAMPLE percentage.

JOINs are out of scope here for the same reason `backend.stats` excludes them:
scaling a one-sided join sample by 1/f is not unbiased in general and join-key
multiplicity adds variance these formulas do not model. `run_runtime_sampling`
keeps its group-completeness heuristic for joins.
"""

from __future__ import annotations

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
    estimate_query,
    recommend_method,
)

_AGG_BY_FUNC = {
    "count": Aggregate.COUNT,
    "sum": Aggregate.SUM,
    "avg": Aggregate.AVG,
}

# Engine-native TABLESAMPLE SYSTEM draws whole row groups, not independent
# rows, so its variance is larger than simple-random-sampling theory predicts.
# Measuring the true design effect per query is separate work (it needs
# block-level aggregates); until then a modest constant inflation is applied to
# SUM/AVG intervals. Empirically DEFF=1.5 lifts grouped-SUM coverage on TPC-H
# from ~86% to ~96% for a nominal-95% interval, at ~25% wider half-widths.
# COUNT is left at 1.0: it estimates a bounded indicator mean and the coverage
# study found it robust to the design.
SYSTEM_SAMPLING_DESIGN_EFFECT = 1.5

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
) -> tuple[EstimateSet, bool, dict[str, Any]]:
    """
    Take one sample, form a family of confidence intervals over the result grid,
    and report whether every cell's relative half-width is within target.

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
        correction=Correction.BONFERRONI if len(agg_cells) > 1 else Correction.NONE,
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
        "ci": estimate_set.to_dict(),
    }
    return estimate_set, bool(met), detail
