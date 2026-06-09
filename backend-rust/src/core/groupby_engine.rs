use crate::core::parser::{AggregateSpec, ParsedQuery};
use std::collections::HashMap;
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct AggregateResult {
    pub result: Vec<Vec<String>>,
    pub rows: Vec<Vec<String>>,
    pub columns: Vec<String>,
    pub result_map: HashMap<String, String>,
}

pub fn render_group_columns(group_by: &[String]) -> String {
    group_by.join(", ")
}

pub fn render_aggregate_sql(aggregate: &AggregateSpec) -> String {
    let expression = aggregate.expression.trim();

    if aggregate.is_count_star() {
        format!("COUNT(*) AS {}", aggregate.alias)
    } else {
        format!(
            "{}({}) AS {}",
            aggregate.func.to_uppercase(),
            expression,
            aggregate.alias
        )
    }
}

pub fn scale_value(
    aggregate: &AggregateSpec,
    value: f64,
    sample_fraction: f64,
) -> f64 {
    match aggregate.func.as_str() {
        "sum" | "count" => value / sample_fraction,
        _ => value,
    }
}

pub fn empty_payload(parsed: &ParsedQuery) -> AggregateResult {
    AggregateResult {
        result: vec![],
        rows: vec![],
        columns: parsed
            .group_by
            .iter()
            .cloned()
            .chain(parsed.aggregates.iter().map(|a| a.alias.clone()))
            .collect(),
        result_map: HashMap::new(),
    }
}

pub fn aggregate_sample(
    _parsed: &ParsedQuery,
    _sample_fraction: f64,
) -> AggregateResult {
    AggregateResult {
        result: vec![],
        rows: vec![],
        columns: vec![],
        result_map: HashMap::new(),
    }
}
