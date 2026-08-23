from __future__ import annotations

import math
import time
from typing import Any, Callable

from backend.core.executor import fetch_sample_frame, fetch_aggregated_sample
from backend.core.groupby_engine import aggregate_sample
from backend.core.parser import ParsedQuery
from backend.core.join_sampling import (
    execute_stratified_join_sample,
    estimate_join_complexity_multiplier,
)


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


def next_adaptive_sample_fraction(
    current_fraction: float,
    confidence: float,
) -> float | None:
    if current_fraction >= 1.0:
        return None

    if confidence < 50:
        next_fraction = current_fraction * 4.0
    elif confidence < 70:
        next_fraction = current_fraction * 2.0
    elif confidence < 90:
        next_fraction = current_fraction * 1.5
    else:
        next_fraction = current_fraction * 1.25

    next_fraction = min(1.0, next_fraction)

    if next_fraction <= current_fraction:
        return None

    return round(next_fraction, 4)


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


def estimate_confidence(
    convergence_error: float,
    sample_fraction: float,
) -> float:
    if math.isinf(convergence_error):
        return 0.0

    confidence_from_error = max(
        0.0,
        min(
            100.0,
            (1.0 - convergence_error) * 100.0,
        ),
    )

    confidence_from_sample = min(
        100.0,
        sample_fraction * 100.0,
    )

    confidence = (
        confidence_from_error * 0.8
        + confidence_from_sample * 0.2
    )

    return round(confidence, 2)


def estimate_query_complexity(parsed: ParsedQuery) -> str:
    score = 0

    score += len(parsed.group_by) * 2

    if parsed.order_by:
        score += len(parsed.order_by)

    if parsed.where_clause:
        score += 2

    score += len(parsed.aggregates)

    for aggregate in parsed.aggregates:
        func = aggregate.func.lower()

        if func in {"avg", "sum"}:
            score += 1

        if func == "count":
            score += 1

        column_text = ""

        if hasattr(aggregate, "column"):
            column_text = str(getattr(aggregate, "column") or "")
        elif hasattr(aggregate, "expression"):
            column_text = str(getattr(aggregate, "expression") or "")

        column_text = column_text.lower()

        if "distinct" in column_text:
            score += 4

    # Use parsed JOIN information for more accurate scoring
    if parsed.has_joins:
        score += len(parsed.joins) * 4

        # Additional penalty for outer joins
        for join in parsed.joins:
            if join.join_type != "INNER":
                score += 2

    query_text = parsed.original_query.lower()

    if " having " in query_text:
        score += 3

    if parsed.limit is None:
        score += 1

    if score <= 4:
        return "simple"

    if score <= 10:
        return "medium"

    return "complex"
 

def run_runtime_sampling(
    parsed: ParsedQuery,
    source: str,
    mode: str,
    accuracy_target: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    mode_key = mode if mode in MODE_CONFIGS else "balanced"
    config = _derive_accuracy_config(mode_key, accuracy_target)

    single_pass_mode = (
        mode_key == "balanced"
        and accuracy_target is None
    )

    complexity = estimate_query_complexity(parsed)

    # Apply JOIN complexity multiplier to time budgets
    join_multiplier = estimate_join_complexity_multiplier(parsed) if parsed.has_joins else 1.0

    if not single_pass_mode:
        if complexity == "simple":
            config["progression"] = [0.005, 0.01, 0.02, 0.05]
            config["time_budget_seconds"] = max(
                config["time_budget_seconds"],
                3.0 * join_multiplier,
            )

        elif complexity == "medium":
            config["progression"] = [0.01, 0.03, 0.05, 0.10, 0.20]
            config["time_budget_seconds"] = max(
                config["time_budget_seconds"],
                6.0 * join_multiplier,
            )

        else:
            config["progression"] = [0.02, 0.05, 0.10, 0.20, 0.40, 0.60]
            config["time_budget_seconds"] = max(
                config["time_budget_seconds"],
                12.0 * join_multiplier,
            )
    else:
        config["progression"] = [0.01]

    start = time.time()
    previous_map: Any = None
    final_payload: dict[str, Any] | None = None
    iteration_details: list[dict[str, Any]] = []
    stop_reason = "progression_exhausted"
    final_error: float | None = None

    progression = list(config["progression"])
    position = 0

    while position < len(progression):
        sample_fraction = progression[position]
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

        # Route to JOIN-specific execution if query contains JOINs
        if parsed.has_joins:
            aggregate_payload, query_time, sample_query = execute_stratified_join_sample(
                parsed,
                source,
                sample_fraction,
            )
            rows_sampled = None
            frame_length = 1
        elif getattr(parsed, "aggregates", None):
            aggregate_payload, query_time, sample_query = fetch_aggregated_sample(
                parsed,
                source,
                sample_fraction,
            )
            rows_sampled = None
            frame_length = 1
        else:
            frame, query_time, sample_query = fetch_sample_frame(
                parsed,
                source,
                sample_fraction,
            )
            aggregate_payload = aggregate_sample(
                frame,
                parsed,
                sample_fraction,
            )
            rows_sampled = int(len(frame))
            frame_length = len(frame)
        convergence_error = _max_convergence_delta(previous_map, aggregate_payload["result_map"])
        confidence = estimate_confidence(
            convergence_error,
            sample_fraction,
        )
        elapsed = time.time() - start

        iteration_detail = {
            "sample_fraction": sample_fraction,
            "rows_sampled": rows_sampled,
            "query_time": query_time,
            "elapsed_time": elapsed,
            "convergence_error": None if math.isinf(convergence_error) else convergence_error,
            "confidence": confidence,
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

        if single_pass_mode:
            stop_reason = "single_pass"
            break

        if (
            len(iteration_details) >= 2
            and frame_length > 0
            and not math.isinf(convergence_error)
            and convergence_error < config["convergence_threshold"]
        ):
            stop_reason = "converged"
            break

        if (
            elapsed >= config["time_budget_seconds"]
            and len(iteration_details) >= 2
        ):
            stop_reason = "time_budget_exceeded"
            break

        if not single_pass_mode:
            adaptive_next = next_adaptive_sample_fraction(
                sample_fraction,
                confidence,
            )

            if (
                adaptive_next is not None
                and adaptive_next not in progression
            ):
                progression.insert(position + 1, adaptive_next)

        position += 1

    if final_payload is None:
        raise RuntimeError("Runtime sampling failed to produce a result")

    total_time = time.time() - start
    final_confidence = estimate_confidence(
        final_error if final_error is not None else math.inf,
        iteration_details[-1]["sample_fraction"],
    )
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
        "query_complexity": complexity,
        "accuracy_target": config.get("accuracy_target"),
        "sample_rate": iteration_details[-1]["sample_fraction"],
        "iterations": iteration_details,
        "convergence_error": None if final_error is None or math.isinf(final_error) else final_error,
        "confidence": final_confidence,
        "estimated_error": (
            None
            if final_error is None or math.isinf(final_error)
            else round(final_error * 100.0, 2)
        ),
        "convergence_threshold": config["convergence_threshold"],
        "stop_reason": stop_reason,
        "rewritten_query": iteration_details[-1]["sample_query"],
    }
