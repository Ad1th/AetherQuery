use mysql_async::{
    prelude::Queryable,
    Pool,
    Row,
};
use std::env;
use std::time::Instant;

#[derive(Debug, Clone)]
pub struct QueryResult {
    pub columns: Vec<String>,
    pub rows: Vec<Vec<String>>,
    pub time: f64,
}

async fn get_pool() -> Result<Pool, String> {
    let host =
        env::var("MYSQL_HOST").unwrap_or_else(|_| "localhost".into());

    let port =
        env::var("MYSQL_PORT").unwrap_or_else(|_| "3306".into());

    let user =
        env::var("MYSQL_USER").unwrap_or_else(|_| "root".into());

    let password =
        env::var("MYSQL_PASSWORD").unwrap_or_else(|_| "root".into());

    let database =
        env::var("MYSQL_DATABASE").unwrap_or_else(|_| "mysql".into());

    let url = format!(
        "mysql://{}:{}@{}:{}/{}",
        user,
        password,
        host,
        port,
        database
    );

    Ok(Pool::new(url.as_str()))
}

fn row_to_strings(row: Row) -> Vec<String> {
    row.unwrap()
        .into_iter()
        .map(|value| format!("{:?}", value))
        .collect()
}

pub async fn execute_query(
    query: &str,
) -> Result<QueryResult, String> {
    let pool = get_pool().await?;

    let mut conn = pool
        .get_conn()
        .await
        .map_err(|e| e.to_string())?;

    let start = Instant::now();

    let rows: Vec<Row> = conn
        .query(query)
        .await
        .map_err(|e| e.to_string())?;

    let elapsed = start.elapsed().as_secs_f64();

    let result_rows = rows
        .into_iter()
        .map(row_to_strings)
        .collect::<Vec<_>>();

    conn.disconnect()
        .await
        .map_err(|e| e.to_string())?;

    Ok(QueryResult {
        columns: vec![],
        rows: result_rows,
        time: elapsed,
    })
}

pub async fn explain_query(
    query: &str,
    analyze: bool,
) -> Result<Vec<Vec<String>>, String> {
    let pool = get_pool().await?;

    let mut conn = pool
        .get_conn()
        .await
        .map_err(|e| e.to_string())?;

    let prefix = if analyze {
        "EXPLAIN ANALYZE"
    } else {
        "EXPLAIN"
    };

    let sql = format!("{} {}", prefix, query);

    let rows: Vec<Row> = conn
        .query(sql)
        .await
        .map_err(|e| e.to_string())?;

    conn.disconnect()
        .await
        .map_err(|e| e.to_string())?;

    Ok(
        rows.into_iter()
            .map(row_to_strings)
            .collect()
    )
}