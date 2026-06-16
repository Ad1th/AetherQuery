from __future__ import annotations

import logging
from typing import Any

from backend.core.parser import AggregateSpec, ParsedQuery
from backend.core.sql import SQLExecutor

logger = logging.getLogger(__name__)


def fetch_aggregated_sample(
    executor: SQLExecutor,
    parsed: ParsedQuery,
    sample_size: int,
) -> list[dict[str, Any]]:
    select_parts = []
    for column in parsed.group_by:
        select_parts.append(column)
    for aggregate in parsed.aggregates:
        if aggregate.is_count_star:
            select_parts.append(f"COUNT(*) AS {aggregate.alias}")
        else:
            select_parts.append(
                f"{aggregate.func.upper()}({aggregate.expression}) AS {aggregate.alias}"
            )

    sql = f"SELECT {', '.join(select_parts)} FROM {parsed.table}"
    if parsed.where_clause:
        sql += f" WHERE {parsed.where_clause}"

    if parsed.group_by:
        sql += f" GROUP BY {', '.join(parsed.group_by)}"

    if parsed.order_by:
        order_by_parts = []
        for order_spec in parsed.order_by:
            direction = "DESC" if order_spec.descending else "ASC"
            order_by_parts.append(f"{order_spec.key} {direction}")
        sql += f" ORDER BY {', '.join(order_by_parts)}"

    if parsed.limit:
        sql += f" LIMIT {parsed.limit}"

    logger.debug(f"Executing SQL: {sql}")
    result = executor.execute(sql)
    return result.fetchall()
