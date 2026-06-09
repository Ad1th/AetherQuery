//NEEDS WORK, fetch_sample_frame() is currently a stub, nowhere to execute SQL from Rust, so need to add SQL engines first.



use crate::core::parser::ParsedQuery;

pub fn sample_clause(source: &str, sample_fraction: f64) -> String {
    let percent = sample_fraction * 100.0;

    match source {
        "postgres" => format!("TABLESAMPLE SYSTEM ({:.4})", percent),
        _ => String::new(),
    }
}

pub fn build_sample_query(
    parsed: &ParsedQuery,
    source: &str,
    sample_fraction: f64,
) -> String {
    let projection_columns = parsed.projection_columns();

    let select_list = if projection_columns.is_empty() {
        "1 AS __aqp_count_marker".to_string()
    } else {
        projection_columns.join(", ")
    };

    let sample_clause = sample_clause(source, sample_fraction);

    let mut where_parts: Vec<String> = Vec::new();

    if let Some(where_clause) = &parsed.where_clause {
        where_parts.push(format!("({})", where_clause));
    }

    match source {
        "duckdb" => {
            where_parts.push(format!(
                "(random() < {:.8})",
                sample_fraction
            ));
        }
        "mysql" => {
            where_parts.push(format!(
                "(RAND() < {:.8})",
                sample_fraction
            ));
        }
        _ => {}
    }

    let mut query = if !sample_clause.is_empty() {
        format!(
            "SELECT {} FROM (SELECT * FROM {} {}) sampled_source",
            select_list,
            parsed.table,
            sample_clause
        )
    } else {
        format!(
            "SELECT {} FROM {}",
            select_list,
            parsed.table
        )
    };

    if !where_parts.is_empty() {
        query.push_str(" WHERE ");
        query.push_str(&where_parts.join(" AND "));
    }

    query
}

#[derive(Debug, Clone)]
pub struct SampleFrameResult {
    pub columns: Vec<String>,
    pub rows: Vec<Vec<String>>,
    pub query_time: f64,
    pub sql: String,
}

pub fn fetch_sample_frame(
    parsed: &ParsedQuery,
    source: &str,
    sample_fraction: f64,
) -> SampleFrameResult {
    let sql = build_sample_query(parsed, source, sample_fraction);

    SampleFrameResult {
        columns: vec![],
        rows: vec![],
        query_time: 0.0,
        sql,
    }
}
