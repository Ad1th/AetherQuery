from __future__ import annotations

import re
from dataclasses import dataclass


SUPPORTED_AGGREGATES = {"count", "sum", "avg"}
SQL_KEYWORDS = {
    "as",
    "asc",
    "avg",
    "by",
    "coalesce",
    "count",
    "desc",
    "from",
    "group",
    "limit",
    "order",
    "select",
    "sum",
    "where",
}


@dataclass(frozen=True)
class AggregateSpec:
    func: str
    expression: str
    alias: str

    @property
    def is_count_star(self) -> bool:
        return self.func == "count" and self.expression == "*"


@dataclass(frozen=True)
class OrderBySpec:
    key: str
    descending: bool = False


@dataclass(frozen=True)
class ParsedQuery:
    raw_sql: str
    table: str
    select_items: list[str]
    aggregates: list[AggregateSpec]
    group_by: list[str]
    where_clause: str | None = None
    order_by: list[OrderBySpec] | None = None
    limit: int | None = None

    @property
    def projection_columns(self) -> list[str]:
        columns = list(dict.fromkeys(self.group_by))
        expression_sources = [*self.group_by]
        if self.where_clause:
            expression_sources.append(self.where_clause)
        for aggregate in self.aggregates:
            if aggregate.expression != "*":
                expression_sources.append(aggregate.expression)
        for expression in expression_sources:
            for identifier in _extract_identifiers(expression):
                if identifier not in columns:
                    columns.append(identifier)
        return columns


def _split_top_level_csv(text: str) -> list[str]:
    items: list[str] = []
    depth = 0
    current: list[str] = []

    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return items


def _normalize_alias(func: str, expression: str) -> str:
    if expression == "*":
        target = "all"
    else:
        target = re.sub(r"[^a-zA-Z0-9_]", "_", expression).strip("_").lower()
    return f"{func.lower()}_{target}"


def _parse_aggregate(item: str) -> AggregateSpec:
    match = re.match(r"(?is)^(count|sum|avg)\s*\(", item.strip())
    if not match:
        raise ValueError(f"Unsupported select expression: {item}")

    func = match.group(1).lower()
    remainder = item.strip()[match.end() :]
    depth = 1
    closing_index = None
    for index, char in enumerate(remainder):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                closing_index = index
                break
    if closing_index is None:
        raise ValueError(f"Malformed aggregate expression: {item}")

    expression = remainder[:closing_index].strip()
    tail = remainder[closing_index + 1 :].strip()
    alias_match = re.match(r"(?is)^(?:as\s+)?([a-zA-Z_][a-zA-Z0-9_]*)?$", tail)
    if not alias_match:
        raise ValueError(f"Unsupported select expression: {item}")
    alias = alias_match.group(1) or _normalize_alias(func, expression)
    if func not in SUPPORTED_AGGREGATES:
        raise ValueError(f"Unsupported aggregate function: {func}")

    return AggregateSpec(func=func, expression=expression, alias=alias)


def _extract_identifiers(expression: str) -> list[str]:
    candidates = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", expression)
    identifiers: list[str] = []
    for token in candidates:
        if token.lower() in SQL_KEYWORDS:
            continue
        if token not in identifiers:
            identifiers.append(token)
    return identifiers


def _parse_order_by(order_by_clause: str | None, parsed: ParsedQuery | None = None) -> list[OrderBySpec]:
    if not order_by_clause:
        return []

    order_specs: list[OrderBySpec] = []
    for item in _split_top_level_csv(order_by_clause):
        match = re.match(r"(?is)^(.+?)(?:\s+(asc|desc))?$", item.strip())
        if not match:
            raise ValueError(f"Unsupported ORDER BY expression: {item}")

        key = match.group(1).strip()
        if parsed is not None:
            for aggregate in parsed.aggregates:
                agg_signature = f"{aggregate.func}({aggregate.expression})"
                if key.lower().replace(" ", "") == agg_signature.replace(" ", ""):
                    key = aggregate.alias
                    break

        order_specs.append(OrderBySpec(key=key, descending=(match.group(2) or "").lower() == "desc"))

    return order_specs


def parse_analytical_query(query: str) -> ParsedQuery:
    normalized = query.strip().rstrip(";")
    match = re.match(
        r"(?is)^select\s+(?P<select>.+?)\s+from\s+(?P<table>[a-zA-Z_][a-zA-Z0-9_]*)"
        r"(?:\s+where\s+(?P<where>.+?))?"
        r"(?:\s+group\s+by\s+(?P<group_by>.+?))?"
        r"(?:\s+order\s+by\s+(?P<order_by>.+?))?"
        r"(?:\s+limit\s+(?P<limit>\d+))?$",
        normalized,
    )
    if not match:
        raise ValueError(
            "Approx mode supports SELECT aggregate queries on one table with optional WHERE/GROUP BY/ORDER BY/LIMIT"
        )

    select_items = _split_top_level_csv(match.group("select"))
    aggregates: list[AggregateSpec] = []
    plain_columns: list[str] = []
    for item in select_items:
        if re.match(r"(?is)^(count|sum|avg)\s*\(", item.strip()):
            aggregates.append(_parse_aggregate(item))
        else:
            plain_columns.append(item.strip())

    if not aggregates:
        raise ValueError("Approx mode requires at least one aggregate expression")

    group_by = _split_top_level_csv(match.group("group_by")) if match.group("group_by") else []
    normalized_plain = [column.lower() for column in plain_columns]
    normalized_group = [column.lower() for column in group_by]
    if normalized_plain != normalized_group:
        raise ValueError("Non-aggregate SELECT columns must match GROUP BY columns in order")

    parsed = ParsedQuery(
        raw_sql=normalized,
        table=match.group("table"),
        select_items=select_items,
        aggregates=aggregates,
        group_by=group_by,
        where_clause=match.group("where").strip() if match.group("where") else None,
        limit=int(match.group("limit")) if match.group("limit") else None,
    )
    order_by = _parse_order_by(match.group("order_by"), parsed=parsed)
    return ParsedQuery(
        raw_sql=parsed.raw_sql,
        table=parsed.table,
        select_items=parsed.select_items,
        aggregates=parsed.aggregates,
        group_by=parsed.group_by,
        where_clause=parsed.where_clause,
        order_by=order_by,
        limit=parsed.limit,
    )
