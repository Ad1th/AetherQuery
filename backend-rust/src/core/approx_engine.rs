use serde_json::Value;

use crate::core::executor::build_sample_query;
use crate::core::parser::parse_analytical_query;
use crate::core::runtime_sampling::{
    run_runtime_sampling,
    mode_configs,
};

pub fn rewrite_agg_query(
    query: &str,
    source: &str,
    mode: &str,
) -> Result<String, String> {
    let source_key = source.trim().to_lowercase();

    let configs = mode_configs();

    let mode_key = if configs.contains_key(mode) {
        mode
    } else {
        "balanced"
    };

    let parsed = parse_analytical_query(query)?;

    let first_fraction = configs
        .get(mode_key)
        .ok_or("Mode config not found")?
        .progression[0];

    Ok(
        build_sample_query(
            &parsed,
            &source_key,
            first_fraction,
        )
    )
}

pub fn run_approx(
    query: &str,
    source: &str,
    mode: &str,
    accuracy_target: Option<f64>,
) -> Result<Value, String> {
    let source_key = source.trim().to_lowercase();

    let parsed =
        parse_analytical_query(query)?;

    let result = run_runtime_sampling(
        &parsed,
        &source_key,
        mode,
        accuracy_target,
    );

    serde_json::to_value(result)
        .map_err(|e| e.to_string())
}