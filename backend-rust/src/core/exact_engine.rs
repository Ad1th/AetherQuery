

use std::time::Instant;

#[derive(Debug, Clone)]
pub struct ExactQueryResult {
    pub result: Vec<Vec<String>>,
    pub columns: Vec<String>,
    pub time: f64,
    pub approx: bool,
    pub source: String,
}

pub fn run_exact(
    query: &str,
    source: &str,
) -> Result<ExactQueryResult, String> {
    let source_key = source.trim().to_lowercase();

    let start = Instant::now();

    match source_key.as_str() {
        "duckdb" | "postgres" | "mysql" => {}
        _ => {
            return Err(format!(
                "Unsupported source: {}",
                source
            ))
        }
    }

    let _query = query;

    let elapsed = start.elapsed().as_secs_f64();

    Ok(ExactQueryResult {
        result: vec![],
        columns: vec![],
        time: elapsed,
        approx: false,
        source: source_key,
    })
}