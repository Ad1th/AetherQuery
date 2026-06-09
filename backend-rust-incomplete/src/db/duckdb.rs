use duckdb::{Connection, Result};
use once_cell::sync::Lazy;
use regex::Regex;
use std::env;
use std::sync::Mutex;
use uuid::Uuid;

#[derive(Debug, Clone)]
pub struct QueryResult {
    pub columns: Vec<String>,
    pub rows: Vec<Vec<String>>,
}

static CONNECTION: Lazy<Mutex<Connection>> = Lazy::new(|| {
    let db_path = env::var("AETHERQUERY_DUCKDB_PATH")
        .unwrap_or_else(|_| "../datasets/aetherquery.duckdb".to_string());

    let conn = Connection::open(db_path)
        .expect("Failed to open DuckDB database");

    Mutex::new(conn)
});

pub fn get_connection() -> &'static Mutex<Connection> {
    &CONNECTION
}

pub fn safe_identifier(name: &str) -> String {
    let re = Regex::new(r"[^a-zA-Z0-9_]").unwrap();

    let mut clean = re.replace_all(name, "_").to_string();

    if clean.is_empty() {
        clean = format!("table_{}", &Uuid::new_v4().to_string()[..8]);
    }

    if clean
        .chars()
        .next()
        .map(|c| c.is_ascii_digit())
        .unwrap_or(false)
    {
        clean = format!("t_{}", clean);
    }

    clean.to_lowercase()
}

pub async fn create_table_from_csv(
    csv_path: &str,
    table_name: Option<&str>,
) -> Result<String> {
    let generated_name = table_name
        .map(|s| s.to_string())
        .unwrap_or_else(|| format!("table_{}", Uuid::new_v4().simple()));

    let safe_name = safe_identifier(&generated_name);

    let escaped_path = csv_path.replace('\'', "''");

    let sql = format!(
        "CREATE OR REPLACE VIEW {} AS SELECT * FROM read_csv_auto('{}');",
        safe_name,
        escaped_path
    );

    let conn = get_connection();
    let conn = conn.lock().unwrap();

    conn.execute_batch(&sql)?;

    Ok(safe_name)
}

pub async fn execute_query(query: &str) -> Result<QueryResult> {
    let conn = get_connection();
    let conn = conn.lock().unwrap();

    let mut stmt = conn.prepare(query)?;

    let column_count = stmt.column_count();

    let columns = stmt
        .column_names()
        .iter()
        .map(|s| s.to_string())
        .collect::<Vec<_>>();

    let mut rows_out = Vec::new();

    let mut rows = stmt.query([])?;

    while let Some(row) = rows.next()? {
        let mut values = Vec::new();

        for idx in 0..column_count {
            let value: Result<String> = row.get(idx);

            values.push(
                value.unwrap_or_else(|_| "<non-string>".to_string())
            );
        }

        rows_out.push(values);
    }

    Ok(QueryResult {
        columns,
        rows: rows_out,
    })
}

pub async fn explain_query(
    query: &str,
    analyze: bool,
) -> Result<QueryResult> {
    let prefix = if analyze {
        "EXPLAIN ANALYZE"
    } else {
        "EXPLAIN"
    };

    execute_query(&format!("{} {}", prefix, query)).await
}
