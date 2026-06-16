from __future__ import annotations

from typing import Any
import time

import pandas as pd

from backend.core.parser import ParsedQuery
from backend.db import duckdb as duckdb_db
from backend.db import mysql as mysql_db
from backend.db import postgres as postgres_db


def _sample_clause(source: str, sample_fraction: float) -> str:
    percent = sample_fraction * 100.0
    if source == "postgres":
        return f"TABLESAMPLE SYSTEM ({percent:.4f})"
    return ""


def build_sample_query(parsed: ParsedQuery, source: str, sample_fraction: float) -> str:
    select_list = ", ".join(parsed.projection_columns) if parsed.projection_columns else "1 AS __aqp_count_marker"
    sample_clause = _sample_clause(source, sample_fraction)

    # For DuckDB, use TABLESAMPLE instead of random() predicates.
    # This avoids evaluating random() on every row and dramatically
    # reduces sampling overhead on large tables.
    if source == "duckdb":
        percent = sample_fraction * 100.0
        query = (
            f"SELECT {select_list} "
            f"FROM {parsed.table} TABLESAMPLE SYSTEM ({percent:.4f} PERCENT)"
        )
        if parsed.where_clause:
            query += f" WHERE ({parsed.where_clause})"
        return query

    where_parts: list[str] = []
    if parsed.where_clause:
        where_parts.append(f"({parsed.where_clause})")

    if source == "mysql":
        where_parts.append(f"(RAND() < {sample_fraction:.8f})")

    if sample_clause:
        query = f"SELECT {select_list} FROM (SELECT * FROM {parsed.table} {sample_clause}) sampled_source"
    else:
        query = f"SELECT {select_list} FROM {parsed.table}"

    if where_parts:
        query = f"{query} WHERE {' AND '.join(where_parts)}"

    return query


def _execute_source_query(query: str, source: str) -> dict[str, Any]:
    if source == "duckdb":
        return duckdb_db.execute_query(query)
    if source == "postgres":
        return postgres_db.execute_query(query)
    if source == "mysql":
        return mysql_db.execute_query(query)
    raise ValueError(f"Unsupported source: {source}")


def fetch_sample_frame(parsed: ParsedQuery, source: str, sample_fraction: float) -> tuple[pd.DataFrame, float, str]:
    sql = build_sample_query(parsed, source, sample_fraction)
    payload = _execute_source_query(sql, source)
    frame = pd.DataFrame(payload.get("rows", []), columns=payload.get("columns", []))
    return frame, float(payload.get("time", 0.0)), sql


def fetch_aggregated_sample(parsed: ParsedQuery, source: str, sample_fraction: float) -> tuple[dict[str, Any], float, str]:
    """
    Execute sampled aggregation directly inside the source engine.
    Avoids materializing hundreds of thousands / millions of sampled rows
    into pandas before aggregation.
    """

    if not parsed.aggregates:
        raise ValueError("fetch_aggregated_sample requires aggregate query metadata")

    if source == "duckdb":
        percent = sample_fraction * 100.0
        from_clause = f"{parsed.table} TABLESAMPLE SYSTEM ({percent:.4f} PERCENT)"
    else:
        sample_clause = _sample_clause(source, sample_fraction)
        if sample_clause:
            from_clause = f"(SELECT * FROM {parsed.table} {sample_clause}) sampled_source"
        else:
            from_clause = parsed.table

    select_parts: list[str] = []

    if getattr(parsed, "group_by", None):
        select_parts.extend(parsed.group_by)

    for aggregate in parsed.aggregates:
        if aggregate.is_count_star:
            select_parts.append(f"COUNT(*) AS {aggregate.alias}")
        else:
            select_parts.append(
                f"{aggregate.func.upper()}({aggregate.expression}) AS {aggregate.alias}"
            )

    sql = f"SELECT {', '.join(select_parts)} FROM {from_clause}"

    if parsed.where_clause:
        sql += f" WHERE ({parsed.where_clause})"

    if getattr(parsed, "group_by", None):
        sql += f" GROUP BY {', '.join(parsed.group_by)}"

    start = time.perf_counter()
    payload = _execute_source_query(sql, source)
    elapsed = time.perf_counter() - start

    rows = payload.get("rows", [])
    columns = payload.get("columns", [])

    result_map: dict[str, Any] = {}

    for index, row in enumerate(rows):
        row_dict = dict(zip(columns, row))

        if getattr(parsed, "group_by", None):
            key = tuple(row_dict[column] for column in parsed.group_by)
            result_map[str(key)] = row_dict
        else:
            result_map[f"row_{index}"] = row_dict

    aggregate_payload = {
        "columns": columns,
        "rows": rows,
        "result_map": result_map,
    }

    query_time = float(payload.get("time", elapsed))
    return aggregate_payload, query_time, sql
