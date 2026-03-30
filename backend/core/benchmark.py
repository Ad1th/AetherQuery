from __future__ import annotations

from typing import Any, Callable

from backend.core.exact_engine import run_exact
from backend.core.parser import ParsedQuery, parse_analytical_query
from backend.core.runtime_sampling import run_runtime_sampling


def _normalize_exact_result(parsed: ParsedQuery, payload: dict[str, Any]) -> Any:
    rows = payload.get("result", [])
    if not parsed.group_by:
        if not rows:
            return {}
        first_row = rows[0]
        return {
            aggregate.alias: first_row[index]
            for index, aggregate in enumerate(parsed.aggregates)
        }

    normalized: dict[Any, dict[str, Any]] = {}
    for row in rows:
        if len(parsed.group_by) == 1:
            key = row[0]
        else:
            key = tuple(row[: len(parsed.group_by)])
        normalized[key] = {
            aggregate.alias: row[len(parsed.group_by) + index]
            for index, aggregate in enumerate(parsed.aggregates)
        }
    return normalized


def _mean_relative_error(exact: Any, approx: Any) -> float | None:
    values: list[float] = []

    if isinstance(exact, dict) and isinstance(approx, dict):
        keys = set(exact) & set(approx)
        if not keys:
            return None
        for key in keys:
            exact_value = exact[key]
            approx_value = approx[key]
            if isinstance(exact_value, dict) and isinstance(approx_value, dict):
                nested_keys = set(exact_value) & set(approx_value)
                for nested_key in nested_keys:
                    reference = exact_value[nested_key]
                    candidate = approx_value[nested_key]
                    if candidate is None:
                        continue
                    if reference in (None, 0):
                        if candidate == reference:
                            values.append(0.0)
                        continue
                    values.append(abs(float(candidate) - float(reference)) / abs(float(reference)))
            else:
                if approx_value is None:
                    continue
                if exact_value in (None, 0):
                    if approx_value == exact_value:
                        values.append(0.0)
                    continue
                values.append(abs(float(approx_value) - float(exact_value)) / abs(float(exact_value)))

    if not values:
        return None
    return sum(values) / len(values)


def run_benchmark(
    query: str,
    source: str,
    approx_mode: str = "balanced",
    accuracy_target: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    parsed = parse_analytical_query(query)
    if progress_callback is not None:
        progress_callback({"phase": "exact", "message": "Running exact query"})
    exact_payload = run_exact(query, source)
    if progress_callback is not None:
        progress_callback({"phase": "approx", "message": "Running approximate query"})
    approx_payload = run_runtime_sampling(
        parsed,
        source,
        approx_mode,
        accuracy_target=accuracy_target,
        progress_callback=progress_callback,
    )

    exact_result_map = _normalize_exact_result(parsed, exact_payload)
    approx_result_map = approx_payload["result_map"]
    error_ratio = _mean_relative_error(exact_result_map, approx_result_map)
    exact_time = float(exact_payload.get("time", 0.0))
    approx_time = float(approx_payload.get("time", 0.0))

    return {
        "benchmark": True,
        "source": source,
        "approx_mode": approx_mode,
        "accuracy_target": accuracy_target,
        "exact": {
            "result": exact_payload.get("result"),
            "columns": exact_payload.get("columns", []),
            "time": exact_time,
        },
        "approx": {
            "result": approx_payload.get("result"),
            "rows": approx_payload.get("rows"),
            "columns": approx_payload.get("columns", []),
            "time": approx_time,
            "sample_rate": approx_payload.get("sample_rate"),
            "accuracy_target": approx_payload.get("accuracy_target"),
            "iterations": approx_payload.get("iterations", []),
            "stop_reason": approx_payload.get("stop_reason"),
        },
        "speedup": (exact_time / approx_time) if approx_time > 0 else None,
        "error_percent": (error_ratio * 100.0) if error_ratio is not None else None,
    }
