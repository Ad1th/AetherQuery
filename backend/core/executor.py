from __future__ import annotations

from typing import Any

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
    where_parts: list[str] = []
    if parsed.where_clause:
        where_parts.append(f"({parsed.where_clause})")
    if source == "duckdb":
        where_parts.append(f"(random() < {sample_fraction:.8f})")
    elif source == "mysql":
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
