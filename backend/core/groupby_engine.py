from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd

from backend.core.parser import AggregateSpec, ParsedQuery


def _render_group_columns(group_by: list[str]) -> str:
    return ", ".join(group_by)


def _render_aggregate_sql(aggregate: AggregateSpec) -> str:
    expression = aggregate.expression.strip()
    if aggregate.is_count_star:
        return f"COUNT(*) AS {aggregate.alias}"
    return f"{aggregate.func.upper()}({expression}) AS {aggregate.alias}"


def _scale_value(aggregate: AggregateSpec, value: Any, sample_fraction: float) -> Any:
    if value is None or pd.isna(value):
        return None
    if aggregate.func in {"sum", "count"}:
        return float(value) / sample_fraction
    return float(value)


def _pythonify(value: Any) -> Any:
    if hasattr(value, "item"):
        try:
            return value.item()
        except ValueError:
            return value
    return value


def _scale_frame(parsed: ParsedQuery, frame: pd.DataFrame, sample_fraction: float) -> pd.DataFrame:
    scaled = frame.copy()
    for aggregate in parsed.aggregates:
        if aggregate.alias not in scaled.columns:
            continue
        if aggregate.func in {"sum", "count"}:
            scaled[aggregate.alias] = scaled[aggregate.alias].astype(float) / sample_fraction
        else:
            scaled[aggregate.alias] = scaled[aggregate.alias].astype(float)
    return scaled


def _format_group_key(row: pd.Series, group_by: list[str]) -> Any:
    if len(group_by) == 1:
        return row[group_by[0]]
    return tuple(row[column] for column in group_by)


def _empty_payload(parsed: ParsedQuery) -> dict[str, Any]:
    if parsed.group_by:
        return {
            "result": [],
            "rows": [],
            "columns": [*parsed.group_by, *[aggregate.alias for aggregate in parsed.aggregates]],
            "result_map": {},
        }

    values: dict[str, Any] = {}
    for aggregate in parsed.aggregates:
        if aggregate.func in {"count", "sum"}:
            values[aggregate.alias] = 0.0
        else:
            values[aggregate.alias] = None
    row = tuple(values[aggregate.alias] for aggregate in parsed.aggregates)
    return {
        "result": row[0] if len(row) == 1 else [row],
        "rows": [row],
        "columns": [aggregate.alias for aggregate in parsed.aggregates],
        "result_map": values,
    }


def aggregate_sample(
    frame: pd.DataFrame,
    parsed: ParsedQuery,
    sample_fraction: float,
) -> dict[str, Any]:
    if frame.empty:
        return _empty_payload(parsed)

    connection = duckdb.connect(database=":memory:")
    try:
        connection.register("sample_frame", frame)

        select_parts: list[str] = []
        if parsed.group_by:
            select_parts.append(_render_group_columns(parsed.group_by))
        select_parts.extend(_render_aggregate_sql(aggregate) for aggregate in parsed.aggregates)

        sql = f"SELECT {', '.join(select_parts)} FROM sample_frame"
        if parsed.group_by:
            sql = f"{sql} GROUP BY {_render_group_columns(parsed.group_by)}"
        if parsed.order_by:
            order_parts = [
                f"{spec.key} {'DESC' if spec.descending else 'ASC'}"
                for spec in parsed.order_by
            ]
            sql = f"{sql} ORDER BY {', '.join(order_parts)}"
        if parsed.limit is not None:
            sql = f"{sql} LIMIT {parsed.limit}"

        result_frame = connection.execute(sql).fetchdf()
    finally:
        connection.close()

    if result_frame.empty:
        return _empty_payload(parsed)

    result_frame = _scale_frame(parsed, result_frame, sample_fraction)

    if not parsed.group_by:
        result_map = {
            aggregate.alias: _pythonify(result_frame.iloc[0][aggregate.alias])
            for aggregate in parsed.aggregates
        }
        row = tuple(result_map[aggregate.alias] for aggregate in parsed.aggregates)
        return {
            "result": row[0] if len(row) == 1 else [row],
            "rows": [row],
            "columns": [aggregate.alias for aggregate in parsed.aggregates],
            "result_map": result_map,
        }

    rows: list[tuple[Any, ...]] = [
        tuple(_pythonify(row[column]) for column in result_frame.columns)
        for _, row in result_frame.iterrows()
    ]
    result_map = {
        _format_group_key(row, parsed.group_by): {
            aggregate.alias: _pythonify(row[aggregate.alias]) for aggregate in parsed.aggregates
        }
        for _, row in result_frame.iterrows()
    }

    return {
        "result": rows,
        "rows": rows,
        "columns": list(result_frame.columns),
        "result_map": result_map,
    }
