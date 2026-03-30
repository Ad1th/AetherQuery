from __future__ import annotations

import math
import time
from typing import Any

from backend.core.executor import fetch_sample_frame
from backend.core.groupby_engine import aggregate_sample
from backend.core.parser import ParsedQuery


MODE_CONFIGS: dict[str, dict[str, Any]] = {
    "fast": {
        "progression": [0.01, 0.05, 0.10],
        "convergence_threshold": 0.08,
        "time_budget_seconds": 0.75,
    },
    "balanced": {
        "progression": [0.01, 0.05, 0.10, 0.25, 0.50],
        "convergence_threshold": 0.04,
        "time_budget_seconds": 1.5,
    },
    "accurate": {
        "progression": [0.02, 0.08, 0.15, 0.30, 0.60, 1.00],
        "convergence_threshold": 0.02,
        "time_budget_seconds": 3.0,
    },
}


def _safe_relative_error(previous: float | int | None, current: float | int | None) -> float:
    if previous is None or current is None:
        return math.inf
    if previous == 0:
        return 0.0 if current == 0 else math.inf
    return abs(float(current) - float(previous)) / abs(float(previous))


def _max_convergence_delta(previous: Any, current: Any) -> float:
    if previous is None:
        return math.inf

    if isinstance(previous, dict) and isinstance(current, dict):
        keys = set(previous) | set(current)
        if not keys:
            return 0.0

        deltas: list[float] = []
        for key in keys:
            prev_value = previous.get(key)
            curr_value = current.get(key)
            if isinstance(prev_value, dict) and isinstance(curr_value, dict):
                nested_keys = set(prev_value) | set(curr_value)
                if not nested_keys:
                    deltas.append(0.0)
                else:
                    deltas.extend(
                        _safe_relative_error(prev_value.get(nested_key), curr_value.get(nested_key))
                        for nested_key in nested_keys
                    )
            else:
                deltas.append(_safe_relative_error(prev_value, curr_value))
        return max(deltas) if deltas else 0.0

    return _safe_relative_error(previous, current)


def run_runtime_sampling(parsed: ParsedQuery, source: str, mode: str) -> dict[str, Any]:
    mode_key = mode if mode in MODE_CONFIGS else "balanced"
    config = MODE_CONFIGS[mode_key]
    start = time.time()
    previous_map: Any = None
    final_payload: dict[str, Any] | None = None
    iteration_details: list[dict[str, Any]] = []
    stop_reason = "progression_exhausted"
    final_error: float | None = None

    for sample_fraction in config["progression"]:
        frame, query_time, sample_query = fetch_sample_frame(parsed, source, sample_fraction)
        aggregate_payload = aggregate_sample(frame, parsed, sample_fraction)
        convergence_error = _max_convergence_delta(previous_map, aggregate_payload["result_map"])
        elapsed = time.time() - start

        iteration_details.append(
            {
                "sample_fraction": sample_fraction,
                "rows_sampled": int(len(frame)),
                "query_time": query_time,
                "elapsed_time": elapsed,
                "convergence_error": None if math.isinf(convergence_error) else convergence_error,
                "sample_query": sample_query,
            }
        )

        final_payload = aggregate_payload
        previous_map = aggregate_payload["result_map"]
        final_error = convergence_error

        if len(frame) > 0 and not math.isinf(convergence_error) and convergence_error < config["convergence_threshold"]:
            stop_reason = "converged"
            break
        if elapsed >= config["time_budget_seconds"]:
            stop_reason = "time_budget_exceeded"
            break

    if final_payload is None:
        raise RuntimeError("Runtime sampling failed to produce a result")

    total_time = time.time() - start
    return {
        **final_payload,
        "time": total_time,
        "approx": True,
        "source": source,
        "mode_profile": mode_key,
        "sample_rate": iteration_details[-1]["sample_fraction"],
        "iterations": iteration_details,
        "convergence_error": None if final_error is None or math.isinf(final_error) else final_error,
        "convergence_threshold": config["convergence_threshold"],
        "stop_reason": stop_reason,
        "rewritten_query": iteration_details[-1]["sample_query"],
    }
