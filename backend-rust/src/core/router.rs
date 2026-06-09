use crate::core::approx_engine::run_approx;
use crate::core::benchmark::run_benchmark;
use crate::core::exact_engine::run_exact;
use serde_json::Value;

pub fn route_query(
    query: &str,
    mode: &str,
    source: &str,
    accuracy_target: Option<f64>,
) -> Result<Value, String> {
    let mode_key = mode.trim().to_lowercase();

    if mode_key == "benchmark" {
        return run_benchmark(
            query,
            source,
            accuracy_target,
        );
    }

    if matches!(
        mode_key.as_str(),
        "approx" | "fast" | "balanced" | "accurate"
    ) {
        let approx_mode = if mode_key == "approx" {
            "balanced"
        } else {
            mode_key.as_str()
        };

        return run_approx(
            query,
            source,
            approx_mode,
            accuracy_target,
        );
    }

    run_exact(query, source)
}