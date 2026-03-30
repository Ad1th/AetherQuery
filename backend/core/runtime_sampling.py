from __future__ import annotations

import math
import time
from typing import Any, Callable

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

BASE_PROGRESSIONS = [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 1.00]


def _derive_accuracy_config(mode: str, accuracy_target: float | None) -> dict[str, Any]:
    mode_key = mode if mode in MODE_CONFIGS else "balanced"
    config = dict(MODE_CONFIGS[mode_key])
    if accuracy_target is None:
        return config

    target = max(50.0, min(99.9, float(accuracy_target)))
    error_budget = max(0.005, min(0.20, 1.0 - (target / 100.0)))
    if target >= 98.0:
        max_fraction = 1.0
    elif target >= 95.0:
        max_fraction = 0.75
    elif target >= 90.0:
        max_fraction = 0.50
    elif target >= 85.0:
        max_fraction = 0.25
    else:
        max_fraction = 0.10

    progression = [fraction for fraction in BASE_PROGRESSIONS if fraction <= max_fraction]
    if not progression or progression[-1] != max_fraction:
        progression.append(max_fraction)

    time_budget = max(0.5, min(5.0, 0.6 + ((target - 50.0) / 49.9) * 4.0))
    config["progression"] = progression
    config["convergence_threshold"] = error_budget
    config["time_budget_seconds"] = time_budget
    config["accuracy_target"] = target
    return config


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


def run_runtime_sampling(
    parsed: ParsedQuery,
    source: str,
    mode: str,
    accuracy_target: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    mode_key = mode if mode in MODE_CONFIGS else "balanced"
    config = _derive_accuracy_config(mode_key, accuracy_target)
    start = time.time()
    previous_map: Any = None
    final_payload: dict[str, Any] | None = None
    iteration_details: list[dict[str, Any]] = []
    stop_reason = "progression_exhausted"
    final_error: float | None = None

    for sample_fraction in config["progression"]:
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "sampling",
                    "message": (
                        f"Sampling {sample_fraction * 100:.0f}% of rows"
                        if accuracy_target is None
                        else f"Sampling {sample_fraction * 100:.0f}% for {config['accuracy_target']:.0f}% target"
                    ),
                    "current_sample_fraction": sample_fraction,
                    "accuracy_target": config.get("accuracy_target"),
                }
            )
        frame, query_time, sample_query = fetch_sample_frame(parsed, source, sample_fraction)
        aggregate_payload = aggregate_sample(frame, parsed, sample_fraction)
        convergence_error = _max_convergence_delta(previous_map, aggregate_payload["result_map"])
        elapsed = time.time() - start

        iteration_detail = {
            "sample_fraction": sample_fraction,
            "rows_sampled": int(len(frame)),
            "query_time": query_time,
            "elapsed_time": elapsed,
            "convergence_error": None if math.isinf(convergence_error) else convergence_error,
            "sample_query": sample_query,
        }
        iteration_details.append(iteration_detail)
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "sampling",
                    "message": f"Processed sample {sample_fraction * 100:.0f}%",
                    "current_sample_fraction": sample_fraction,
                    "accuracy_target": config.get("accuracy_target"),
                    "latest_iteration": iteration_detail,
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
    if progress_callback is not None:
        progress_callback(
            {
                "phase": "finalizing",
                "message": "Finalizing approximate result",
                "current_sample_fraction": iteration_details[-1]["sample_fraction"],
                "accuracy_target": config.get("accuracy_target"),
            }
        )
    return {
        **final_payload,
        "time": total_time,
        "approx": True,
        "source": source,
        "mode_profile": mode_key,
        "accuracy_target": config.get("accuracy_target"),
        "sample_rate": iteration_details[-1]["sample_fraction"],
        "iterations": iteration_details,
        "convergence_error": None if final_error is None or math.isinf(final_error) else final_error,
        "convergence_threshold": config["convergence_threshold"],
        "stop_reason": stop_reason,
        "rewritten_query": iteration_details[-1]["sample_query"],
    }
