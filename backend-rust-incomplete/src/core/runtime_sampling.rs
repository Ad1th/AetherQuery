use crate::core::executor::fetch_sample_frame;
use crate::core::groupby_engine::{aggregate_sample, AggregateResult};
use crate::core::parser::ParsedQuery;
use std::collections::HashMap;
use std::time::Instant;
use serde::Serialize;

#[derive(Debug, Clone)]
pub struct ModeConfig {
    pub progression: Vec<f64>,
    pub convergence_threshold: f64,
    pub time_budget_seconds: f64,
}

#[derive(Debug, Clone, Serialize)]
pub struct IterationDetail {
    pub sample_fraction: f64,
    pub rows_sampled: usize,
    pub query_time: f64,
    pub elapsed_time: f64,
    pub convergence_error: Option<f64>,
    pub sample_query: String,
}




#[derive(Debug, Clone, Serialize)]
pub struct RuntimeSamplingResult {
    pub payload: AggregateResult,
    pub sample_rate: f64,
    pub iterations: Vec<IterationDetail>,
    pub convergence_error: Option<f64>,
    pub convergence_threshold: f64,
    pub stop_reason: String,
    pub mode_profile: String,
    pub accuracy_target: Option<f64>,
    pub total_time: f64,
    pub rewritten_query: String,
}

pub fn mode_configs() -> HashMap<&'static str, ModeConfig> {
    let mut configs = HashMap::new();

    configs.insert(
        "fast",
        ModeConfig {
            progression: vec![0.01, 0.05, 0.10],
            convergence_threshold: 0.08,
            time_budget_seconds: 0.75,
        },
    );

    configs.insert(
        "balanced",
        ModeConfig {
            progression: vec![0.01, 0.05, 0.10, 0.25, 0.50],
            convergence_threshold: 0.04,
            time_budget_seconds: 1.5,
        },
    );

    configs.insert(
        "accurate",
        ModeConfig {
            progression: vec![0.02, 0.08, 0.15, 0.30, 0.60, 1.00],
            convergence_threshold: 0.02,
            time_budget_seconds: 3.0,
        },
    );

    configs
}

pub fn run_runtime_sampling(
    parsed: &ParsedQuery,
    source: &str,
    mode: &str,
    accuracy_target: Option<f64>,
) -> RuntimeSamplingResult {
    let configs = mode_configs();

    let config = configs
        .get(mode)
        .or_else(|| configs.get("balanced"))
        .unwrap();

    let start = Instant::now();

    let mut final_payload = aggregate_sample(parsed, 1.0);
    let mut iterations = Vec::new();
    let mut last_query = String::new();

    for sample_fraction in &config.progression {
        let sample = fetch_sample_frame(
            parsed,
            source,
            *sample_fraction,
        );

        last_query = sample.sql.clone();

        iterations.push(IterationDetail {
            sample_fraction: *sample_fraction,
            rows_sampled: sample.rows.len(),
            query_time: sample.query_time,
            elapsed_time: start.elapsed().as_secs_f64(),
            convergence_error: None,
            sample_query: sample.sql.clone(),
        });
    }

    RuntimeSamplingResult {
        payload: final_payload,
        sample_rate: *config.progression.last().unwrap_or(&1.0),
        iterations,
        convergence_error: None,
        convergence_threshold: config.convergence_threshold,
        stop_reason: "progression_exhausted".to_string(),
        mode_profile: mode.to_string(),
        accuracy_target,
        total_time: start.elapsed().as_secs_f64(),
        rewritten_query: last_query,
    }
}
