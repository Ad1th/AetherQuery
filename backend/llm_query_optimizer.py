from __future__ import annotations

import argparse
import os
import re
import subprocess
import time
from typing import Any

import psycopg

# ------------------------------
# Schema (lineitem table)
# ------------------------------
LINEITEM_SCHEMA = """
lineitem(
    l_orderkey INT,
    l_partkey INT,
    l_suppkey INT,
    l_linenumber INT,
    l_quantity NUMERIC,
    l_extendedprice NUMERIC,
    l_discount NUMERIC,
    l_tax NUMERIC,
    l_returnflag TEXT,
    l_linestatus TEXT,
    l_shipdate DATE,
    l_commitdate DATE,
    l_receiptdate DATE,
    l_shipinstruct TEXT,
    l_shipmode TEXT,
    l_comment TEXT
)
"""


def sanitize_llm_sql(query: str) -> str:
    query = re.sub(r"--.*", "", query)
    query = re.sub(r"/\*.*?\*/", "", query, flags=re.DOTALL)
    query = re.sub(r"^```sql\s*|```$", "", query, flags=re.MULTILINE).strip()

    match = re.search(
        r"(SELECT|WITH|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b.*;",
        query,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        query = match.group(0)

    columns = [
        "orderkey",
        "partkey",
        "suppkey",
        "linenumber",
        "quantity",
        "extendedprice",
        "discount",
        "tax",
        "returnflag",
        "linestatus",
        "shipdate",
        "commitdate",
        "receiptdate",
        "shipinstruct",
        "shipmode",
        "comment",
    ]
    for col in columns:
        query = re.sub(rf"\b{col}\b", f"l_{col}", query, flags=re.IGNORECASE)

    query = " ".join(line.strip() for line in query.splitlines() if line.strip())
    return query


def get_pg_conn(
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> psycopg.Connection:
    return psycopg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        dbname=database,
    )


def run_query(
    query: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> tuple[list[tuple[Any, ...]], float]:
    with get_pg_conn(host, port, user, password, database) as conn:
        with conn.cursor() as cur:
            start = time.time()
            cur.execute(query)
            rows = cur.fetchall() if cur.description else []
            elapsed = time.time() - start
            return rows, elapsed


def optimize_query_with_llm(
    query: str,
    model: str,
) -> tuple[str | None, float]:
    prompt = f"""
Optimize the following SQL query for PostgreSQL. Table definition:

{LINEITEM_SCHEMA}

Query:
{query}

Return only the optimized SQL.
"""
    start = time.time()
    process = subprocess.run(
        ["ollama", "run", model],
        input=prompt.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    llm_time = time.time() - start

    if process.returncode != 0:
        print("Ollama error:", process.stderr.decode("utf-8", errors="ignore"))
        return None, llm_time

    return process.stdout.decode("utf-8", errors="ignore").strip(), llm_time


def test_query(
    query: str,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    model: str,
) -> None:
    print("Original Query:\n", query.strip(), "\n")

    try:
        orig_results, orig_time = run_query(query, host, port, user, password, database)
    except Exception as exc:
        print("Original query failed:", exc)
        return

    print("Execution Time (Original Query):", round(orig_time, 4), "seconds\n")

    optimized_query, llm_time = optimize_query_with_llm(query, model=model)
    if not optimized_query:
        print("LLM did not return a suggestion.")
        return

    optimized_query = sanitize_llm_sql(optimized_query)
    print("Optimized Query:\n", optimized_query, "\n")

    try:
        results, exec_time = run_query(
            optimized_query, host, port, user, password, database
        )
    except Exception as exc:
        print("Optimized query failed:", exc)
        print("LLM Optimization Time:", round(llm_time, 4), "seconds\n")
        return

    print("Execution Time (Optimized Query):", round(exec_time, 4), "seconds")
    print("LLM Optimization Time:", round(llm_time, 4), "seconds")
    print("Result row count (Original):", len(orig_results))
    print("Result row count (Optimized):", len(results), "\n")

    for row in results[:50]:
        print(row)
    if len(results) > 50:
        print(f"... ({len(results) - 50} more rows)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize Postgres SQL using Ollama")
    parser.add_argument(
        "--query",
        default="SELECT SUM(l_suppkey) FROM lineitem;",
        help="SQL query to optimize and benchmark",
    )
    parser.add_argument("--model", default="phi3:mini", help="Ollama model")
    parser.add_argument("--host", default=os.getenv("PGHOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PGPORT", "5432")))
    parser.add_argument("--user", default=os.getenv("PGUSER", "tpch_user"))
    parser.add_argument("--password", default=os.getenv("PGPASSWORD", "abcd@1234"))
    parser.add_argument("--database", default=os.getenv("PGDATABASE", "tpch"))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    test_query(
        query=args.query,
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        model=args.model,
    )
