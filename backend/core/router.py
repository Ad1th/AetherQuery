from typing import Any

from backend.core.benchmark import run_benchmark
from backend.core.approx_engine import run_approx
from backend.core.exact_engine import run_exact


def route_query(query: str, mode: str, source: str) -> dict[str, Any]:
    mode_key = mode.lower().strip()
    if mode_key == "benchmark":
        return run_benchmark(query, source)
    if mode_key in {"approx", "fast", "balanced", "accurate"}:
        approx_mode = "balanced" if mode_key == "approx" else mode_key
        return run_approx(query, source, mode=approx_mode)
    return run_exact(query, source)
