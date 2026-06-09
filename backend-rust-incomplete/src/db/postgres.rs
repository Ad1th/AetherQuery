// src/db/postgres.rs

use std::env;
use std::time::Instant;

use serde::{Deserialize, Serialize};
use tokio_postgres::{Client, NoTls, Row};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueryResult {
    pub columns: Vec<String>,
    pub rows: Vec<Vec<String>>,
    pub time: f64,
}

async fn get_connection() -> Result<Client, String> {
    let host =
        env::var("PGHOST").unwrap_or_else(|_| "localhost".to_string());

    let port =
        env::var("PGPORT").unwrap_or_else(|_| "5432".to_string());

    let database =
        env::var("PGDATABASE").unwrap_or_else(|_| "tpch".to_string());

    let user =
        env::var("PGUSER").unwrap_or_else(|_| "postgres".to_string());

    let password =
        env::var("PGPASSWORD").unwrap_or_default();

    let conn_string = format!(
        "host={} port={} dbname={} user={} password={}",
        host,
        port,
        database,
        user,
        password
    );

    let (client, connection) =
        tokio_postgres::connect(
            &conn_string,
            NoTls,
        )
        .await
        .map_err(|e| {
            format!(
                "Postgres connection failed: {}",
                e
            )
        })?;

    tokio::spawn(async move {
        if let Err(e) = connection.await {
            eprintln!(
                "postgres connection error: {}",
                e
            );
        }
    });

    Ok(client)
}

fn cell_to_string(row: &Row, idx: usize) -> String {
    if let Ok(v) = row.try_get::<usize, String>(idx) {
        return v;
    }
    if let Ok(v) = row.try_get::<usize, &str>(idx) {
        return v.to_string();
    }
    if let Ok(v) = row.try_get::<usize, i64>(idx) {
        return v.to_string();
    }
    if let Ok(v) = row.try_get::<usize, i32>(idx) {
        return v.to_string();
    }
    if let Ok(v) = row.try_get::<usize, f64>(idx) {
        return v.to_string();
    }
    if let Ok(v) = row.try_get::<usize, bool>(idx) {
        return v.to_string();
    }

    "NULL".to_string()
}

pub async fn execute_query(
    query: &str,
) -> Result<QueryResult, String> {
    let client = get_connection().await?;

    let start = Instant::now();

    let rows = client
        .query(query, &[])
        .await
        .map_err(|e| e.to_string())?;

    let duration =
        start.elapsed().as_secs_f64();

    let columns = rows
        .first()
        .map(|row| {
            row.columns()
                .iter()
                .map(|c| c.name().to_string())
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();

    let mut result_rows = Vec::new();

    for row in rows {
        let mut values = Vec::new();
        for idx in 0..row.len() {
            values.push(cell_to_string(&row, idx));
        }
        result_rows.push(values);
    }

    Ok(QueryResult {
        columns,
        rows: result_rows,
        time: duration,
    })
}

pub async fn explain_query(
    query: &str,
    analyze: bool,
) -> Result<String, String> {
    let client = get_connection().await?;

    let prefix = if analyze {
        "EXPLAIN (ANALYZE, FORMAT JSON)"
    } else {
        "EXPLAIN (FORMAT JSON)"
    };

    let sql =
        format!("{} {}", prefix, query);

    let rows = client
        .query(&sql, &[])
        .await
        .map_err(|e| e.to_string())?;

    if rows.is_empty() {
        return Ok(String::new());
    }

    let plan: String =
        rows[0].try_get(0)
            .map_err(|e| e.to_string())?;

    Ok(plan)
}