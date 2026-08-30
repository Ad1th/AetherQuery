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
    hll_guided_join_min_rate,
)
from backend.core.sufficient_stats import (
    evaluate_sample_accuracy,
    evaluate_join_sample_accuracy,
    expected_group_count,
    join_ci_is_defensible,
)
from backend.stats.sequential import Schedule, alpha_for_look


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


def _safe_relative_error(previous: Any, current: Any) -> float:
    if previous is None or current is None:
        return math.inf
    if isinstance(previous, (str, bool)) or isinstance(current, (str, bool)):
        return 0.0 if previous == current else math.inf
    try:
        prev_f = float(previous)
        curr_f = float(current)
    except (ValueError, TypeError):
        return 0.0 if previous == current else math.inf

    if prev_f == 0:
        return 0.0 if curr_f == 0 else math.inf
    return abs(curr_f - prev_f) / abs(prev_f)


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
 

def resolve_sampling_plan(
    parsed: ParsedQuery,
    source: str,
    mode: str,
    accuracy_target: float | None = None,
    *,
    single_pass_mode: bool = False,
) -> dict[str, Any]:
    """
    Resolve the sample-size ladder and time budget the controller will walk.

    Factored out of `run_runtime_sampling` so that an alternative stopping
    policy (the online-aggregation-style baseline used in the evaluation) can
    be driven over exactly the same ladder, by construction rather than by
    a transcribed copy. The returned dict is the mode config with
    `progression` and `time_budget_seconds` resolved, plus the join-rate
    diagnostic.
    """
    mode_key = mode if mode in MODE_CONFIGS else "balanced"
    config = _derive_accuracy_config(mode_key, accuracy_target)
    complexity = estimate_query_complexity(parsed)

    # Apply JOIN complexity multiplier to time budgets
    join_multiplier = estimate_join_complexity_multiplier(parsed) if parsed.has_joins else 1.0

    # Starting sample rate for JOIN queries. Sampling one side of a selective
    # join at 1-2% frequently yields zero matching rows, so the adaptive loop
    # never sees a usable estimate and still pays full TABLESAMPLE overhead
    # (slower than exact for no result). A fixed floor scales with the join
    # count; HyperLogLog cardinality sketches of the join keys then lift it to
    # whatever rate actually delivers enough matched rows per output group.
    join_min_sample_rate = 0.0
    join_rate_diag: dict[str, Any] | None = None
    if parsed.has_joins and not single_pass_mode:
        num_joins = len(parsed.joins) if parsed.joins else 0
        if num_joins >= 3:
            join_min_sample_rate = 0.10
        elif num_joins >= 2:
            join_min_sample_rate = 0.07
        else:
            join_min_sample_rate = 0.05
        try:
            join_min_sample_rate, join_rate_diag = hll_guided_join_min_rate(
                parsed, source, floor=join_min_sample_rate
            )
        except Exception:
            join_rate_diag = {"method": "fixed_floor", "floor": join_min_sample_rate}

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

        # Apply the JOIN floor last so the complexity presets above cannot
        # silently drop the progression back down to 1-2% for join queries.
        if join_min_sample_rate > 0.0:
            floored = [s for s in config["progression"] if s >= join_min_sample_rate]
            if not floored or floored[0] > join_min_sample_rate:
                floored.insert(0, join_min_sample_rate)
            config["progression"] = floored
    else:
        config["progression"] = [0.01]
    config["join_min_sample_rate"] = join_min_sample_rate
    config["join_rate_diag"] = join_rate_diag
    config["complexity"] = complexity
    return config


def run_runtime_sampling(
    parsed: ParsedQuery,
    source: str,
    mode: str,
    accuracy_target: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ci_multiplicity_correction: bool = True,
    ci_anytime_valid: bool = True,
    ci_coverage_level: float = 0.95,
) -> dict[str, Any]:
    mode_key = mode if mode in MODE_CONFIGS else "balanced"
    config = _derive_accuracy_config(mode_key, accuracy_target)

    # CRITICAL FIX: Never use single-pass mode for JOIN queries
    # JOINs require adaptive progression to find matching rows at optimal sample rates
    single_pass_mode = (
        mode_key == "balanced"
        and accuracy_target is None
        and not parsed.has_joins  # Force multi-iteration for JOINs
    )

    # Confidence-interval stopping: stop when a real Bonferroni-corrected CI
    # over the whole result grid is inside the error budget, not when two
    # consecutive samples happen to agree (stability is not accuracy).
    #
    # Non-JOIN aggregates use the single-table estimators. INNER equi-joins that
    # are fact -> dimension in shape (join_ci_is_defensible) use the cluster
    # estimator: the sampled fact table's physical row group is the sampling
    # unit, so the between-block variance absorbs the 1:N fan-out. A naive 1/f
    # expansion here was measured to under-cover at 0-60%; the cluster path
    # measures 95-100%. Other joins keep the group-completeness heuristic.
    ci_join = parsed.has_joins and join_ci_is_defensible(parsed)
    use_ci_stop = bool(getattr(parsed, "aggregates", None)) and (
        not parsed.has_joins or ci_join
    )
    if accuracy_target is not None:
        ci_target_error = max(0.005, min(0.5, 1.0 - float(accuracy_target) / 100.0))
    else:
        ci_target_error = float(config["convergence_threshold"])
    # A single 1% block sample with no interval is exactly the unreliable case
    # CI stopping exists to fix, so never short-circuit it.
    single_pass_mode = single_pass_mode and not use_ci_stop

    _plan = resolve_sampling_plan(
        parsed, source, mode_key, accuracy_target, single_pass_mode=single_pass_mode
    )
    config["progression"] = _plan["progression"]
    config["time_budget_seconds"] = _plan["time_budget_seconds"]
    join_min_sample_rate = _plan["join_min_sample_rate"]
    join_rate_diag = _plan["join_rate_diag"]
    complexity = _plan["complexity"]

    start = time.time()
    previous_map: Any = None
    final_payload: dict[str, Any] | None = None
    iteration_details: list[dict[str, Any]] = []
    stop_reason = "progression_exhausted"
    final_error: float | None = None
    max_groups_seen = 0
    previous_groups_returned: int | None = None
    # A high-cardinality GROUP BY over a join (one row per customer, say) can
    # only ever show every group at a near-full scan, so an unbounded "missing
    # groups -> escalate" rule would make every such query slower than exact.
    # Chase the missing groups for at most this many iterations, then stop and
    # report the result as incomplete rather than grinding up to 100%.
    max_incomplete_iterations = 4
    # Past this many output groups a query is not an approximation candidate --
    # you cannot sample your way to hundreds of thousands of distinct group
    # keys -- so stop early and hand back a labelled best-effort result.
    aqp_group_ceiling = 50_000
    # Result-grid completeness for single-table grouped queries. The family-wise
    # certificate is over the groups in the grid; a group absent from the sample
    # is silently absent from the grid. `expected_group_count` returns the exact
    # population group count when it is cheap (no predicate, or only IS NOT NULL
    # on the grouping columns) -- then the grid is certified only once every
    # group has been observed. When it cannot be had cheaply (a predicate on a
    # non-grouping column), fall back to requiring the discovered group set to be
    # non-decreasing and unchanged for a full look before certifying, and label
    # the result `groups_incomplete` if that never holds within the budget.
    single_table_grid = bool(
        use_ci_stop and not ci_join and getattr(parsed, "group_by", None)
    )
    expected_groups: int | None = None
    if single_table_grid:
        try:
            expected_groups = expected_group_count(parsed, source)
        except Exception:
            expected_groups = None

    progression = list(config["progression"])
    position = 0
    last_ci_block: dict[str, Any] | None = None
    # Anytime-valid stopping: the CI is re-checked at every look, which is
    # optional stopping and makes a fixed 95% interval optimistic. Spend the
    # 5% error budget across looks with a harmonic schedule (the right choice
    # here -- each look re-draws an independent TABLESAMPLE, so there is no
    # martingale for a tighter stream construction, and the controller inserts
    # looks adaptively). Each look's interval is then computed at a higher
    # coverage level so that ALL looks hold simultaneously at 95%.
    ci_look = 0
    ci_look_coverage = ci_coverage_level

    while position < len(progression):
        sample_fraction = progression[position]
        ci_met = False
        ci_max_rel_hw: float | None = None
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
        if parsed.has_joins and not ci_join:
            aggregate_payload, query_time, sample_query = execute_stratified_join_sample(
                parsed,
                source,
                sample_fraction,
            )
            rows_sampled = None
            frame_length = 1
        elif use_ci_stop:
            _evaluator = evaluate_join_sample_accuracy if ci_join else evaluate_sample_accuracy
            ci_look += 1
            if ci_anytime_valid:
                alpha_t = alpha_for_look(Schedule.HARMONIC, 1.0 - ci_coverage_level, ci_look)
                ci_look_coverage = 1.0 - alpha_t
            else:
                ci_look_coverage = ci_coverage_level
            try:
                _estimate_set, ci_met, ci_detail = _evaluator(
                    parsed,
                    source,
                    sample_fraction,
                    coverage_level=ci_look_coverage,
                    target_relative_error=ci_target_error,
                    multiplicity_correction=ci_multiplicity_correction,
                )
            except Exception:
                # Never let the interval layer fail the query: fall back to the
                # pushed-down sampled aggregate (join-aware) for this iteration
                # and let the progression / time budget carry the stop decision.
                was_join = ci_join
                use_ci_stop = False
                ci_join = False
                if was_join:
                    aggregate_payload, query_time, sample_query = execute_stratified_join_sample(
                        parsed, source, sample_fraction
                    )
                else:
                    aggregate_payload, query_time, sample_query = fetch_aggregated_sample(
                        parsed, source, sample_fraction
                    )
                rows_sampled = None
                frame_length = 1
                ci_met = False
                ci_max_rel_hw = None
            else:
                aggregate_payload = {
                    "columns": ci_detail["columns"],
                    "rows": ci_detail["rows"],
                    "result_map": ci_detail["result_map"],
                }
                query_time = ci_detail["query_time"]
                sample_query = ci_detail["sample_query"]
                rows_sampled = ci_detail["n_sample"]
                frame_length = 1
                ci_max_rel_hw = ci_detail["max_relative_half_width"]
                last_ci_block = ci_detail["ci"]
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

        # Track group coverage. For JOIN queries a small sample can miss whole
        # GROUP BY groups entirely; an answer with fewer groups than a larger
        # sample already produced is not "converged", it is under-sampled.
        groups_returned = len(aggregate_payload.get("result_map", {}))
        max_groups_seen = max(max_groups_seen, groups_returned)
        # Result-grid completeness. A tight interval on the groups a sample
        # returned says nothing about groups it missed.
        if single_table_grid:
            if groups_returned == 0:
                groups_incomplete = True
            elif expected_groups is not None:
                # exact population group count known: complete only once every
                # group has been observed
                groups_incomplete = groups_returned < expected_groups
            else:
                # no cheap exact count: require the discovered group set to be
                # non-decreasing and unchanged since the previous look
                groups_incomplete = (
                    groups_returned < max_groups_seen
                    or previous_groups_returned is None
                    or groups_returned != previous_groups_returned
                )
        else:
            # JOIN queries: a small fact sample can miss whole dimension groups.
            groups_incomplete = bool(
                parsed.has_joins
                and (
                    groups_returned == 0
                    or groups_returned < max_groups_seen
                    or (previous_groups_returned is not None and groups_returned > previous_groups_returned)
                )
            )
        # Keep chasing missing groups only for a bounded number of iterations.
        completeness_blocks_stop = (
            groups_incomplete and len(iteration_details) < max_incomplete_iterations
        )

        iteration_detail = {
            "sample_fraction": sample_fraction,
            "rows_sampled": rows_sampled,
            "query_time": query_time,
            "elapsed_time": elapsed,
            "convergence_error": None if math.isinf(convergence_error) else convergence_error,
            "confidence": confidence,
            "ci_met": ci_met,
            "ci_look_coverage": ci_look_coverage if use_ci_stop else None,
            "ci_max_relative_half_width": ci_max_rel_hw,
            "groups_returned": groups_returned,
            "groups_incomplete": groups_incomplete,
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
        previous_groups_returned = groups_returned
        final_error = convergence_error

        if single_pass_mode:
            stop_reason = "single_pass"
            break

        # Confidence-interval stopping path. For defensible INNER-equi joins the
        # completeness guard still applies: a small fact sample can miss whole
        # dimension groups, and a tight interval on the groups it did see says
        # nothing about the ones it missed.
        if use_ci_stop:
            if ci_met and not groups_incomplete:
                stop_reason = "ci_within_target"
                break
            if (
                elapsed >= config["time_budget_seconds"]
                and len(iteration_details) >= 2
                and not groups_incomplete
            ):
                stop_reason = "time_budget_exceeded"
                break
            if ci_join and groups_incomplete and groups_returned >= aqp_group_ceiling:
                stop_reason = "groups_incomplete"
                break
            # Single-table grid whose group set never settled within the budget:
            # hand back a labelled best-effort result rather than grind to a
            # full scan.
            if (
                single_table_grid
                and groups_incomplete
                and len(iteration_details) >= max_incomplete_iterations
            ):
                stop_reason = "groups_incomplete"
                break
            # Grow the next sample in proportion to how far the widest interval
            # still is from the target: far away -> 4x, close -> 1.25x. Drive
            # the ladder purely off this so the sequence stays monotone. While
            # dimension groups are still missing, ignore the (misleadingly tight)
            # interval on the groups seen so far and escalate hard.
            if groups_incomplete:
                synth_conf = 0.0
            elif ci_max_rel_hw and ci_max_rel_hw > 0:
                synth_conf = max(0.0, min(100.0, (ci_target_error / ci_max_rel_hw) * 100.0))
            else:
                synth_conf = 95.0
            adaptive_next = next_adaptive_sample_fraction(sample_fraction, synth_conf)
            if adaptive_next is None:
                stop_reason = "progression_exhausted"
                break
            progression[position + 1:] = [adaptive_next]
            position += 1
            continue

        # A JOIN result that is still missing groups must not be allowed to
        # stop on convergence or the time budget: escalate the sample rate
        # aggressively until the group set stabilises or the budget is spent.
        if completeness_blocks_stop and sample_fraction < 1.0:
            forced_next = min(1.0, max(round(sample_fraction * 4.0, 4), 0.25))
            if forced_next > sample_fraction:
                if position + 1 >= len(progression) or progression[position + 1] < forced_next:
                    progression.insert(position + 1, forced_next)
                position += 1
                continue

        if parsed.has_joins and groups_returned >= aqp_group_ceiling:
            # Far too many groups for sampling to ever complete; don't burn
            # further iterations climbing toward a full scan.
            stop_reason = "groups_incomplete"
            break

        if groups_incomplete and not completeness_blocks_stop:
            # Budget spent and groups still missing: stop here rather than
            # grinding to a full scan, and label the result for what it is.
            stop_reason = "groups_incomplete"
            break

        if (
            len(iteration_details) >= 2
            and frame_length > 0
            and not groups_incomplete
            and not math.isinf(convergence_error)
            and convergence_error < config["convergence_threshold"]
        ):
            stop_reason = "converged"
            break

        if (
            elapsed >= config["time_budget_seconds"]
            and len(iteration_details) >= 2
            and not groups_incomplete
        ):
            stop_reason = "time_budget_exceeded"
            break

        # Skip the confidence-driven refinement step while groups are still
        # incomplete: convergence_error is meaningless across a changing group
        # set, so it would otherwise insert a new fraction every iteration and
        # grind through a dozen samples. Forced escalation above handles it.
        if not single_pass_mode and not groups_incomplete:
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
    last_iter = iteration_details[-1]
    final_confidence = estimate_confidence(
        final_error if final_error is not None else math.inf,
        last_iter["sample_fraction"],
    )
    if use_ci_stop:
        # A genuine coverage statement replaces the pseudo-confidence heuristic.
        final_confidence = 95.0 if stop_reason == "ci_within_target" else round(
            min(94.0, 100.0 * (ci_target_error / last_iter["ci_max_relative_half_width"]))
            if last_iter.get("ci_max_relative_half_width")
            else 0.0,
            2,
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
            round(last_iter["ci_max_relative_half_width"] * 100.0, 3)
            if use_ci_stop and last_iter.get("ci_max_relative_half_width") is not None
            else (
                None
                if final_error is None or math.isinf(final_error)
                else round(final_error * 100.0, 2)
            )
        ),
        "convergence_threshold": config["convergence_threshold"],
        "target_relative_error": ci_target_error if use_ci_stop else None,
        "ci": last_ci_block,
        "anytime_valid": use_ci_stop and ci_anytime_valid,
        "ci_looks": ci_look,
        "ci_final_look_coverage": ci_look_coverage if use_ci_stop else None,
        "join_rate_selection": join_rate_diag,
        "stop_reason": stop_reason,
        "groups_returned": last_iter["groups_returned"],
        "groups_incomplete": last_iter["groups_incomplete"],
        "rewritten_query": last_iter["sample_query"],
    }
