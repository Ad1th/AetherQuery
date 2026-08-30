from __future__ import annotations

from typing import Any, Callable

from backend.core.executor import build_sample_query
from backend.core.parser import parse_analytical_query
from backend.core.runtime_sampling import MODE_CONFIGS, run_runtime_sampling


def _rewrite_agg_query(query: str, source: str, mode: str = "balanced") -> str:
    source_key = source.lower().strip()
    mode_key = mode if mode in MODE_CONFIGS else "balanced"
    parsed = parse_analytical_query(query)
    first_fraction = MODE_CONFIGS[mode_key]["progression"][0]
    return build_sample_query(parsed, source_key, first_fraction)


def run_approx(
    query: str,
    source: str = "duckdb",
    mode: str = "balanced",
    accuracy_target: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ci_multiplicity_correction: bool = True,
    ci_anytime_valid: bool = True,
    ci_coverage_level: float = 0.95,
) -> dict[str, Any]:
    source_key = source.lower().strip()
    parsed = parse_analytical_query(query)
    return run_runtime_sampling(
        parsed,
        source_key,
        mode,
        accuracy_target=accuracy_target,
        progress_callback=progress_callback,
        ci_multiplicity_correction=ci_multiplicity_correction,
        ci_anytime_valid=ci_anytime_valid,
        ci_coverage_level=ci_coverage_level,
    )
