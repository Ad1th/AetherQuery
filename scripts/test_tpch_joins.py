"""
Test approximate JOIN execution on TPC-H queries.
Compares approximate vs exact results for accuracy and speedup.
"""

import sys
import time
import os

# Add backend to path
backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'backend')
sys.path.insert(0, backend_path)

from core.parser import parse_analytical_query
from core.runtime_sampling import run_runtime_sampling
from db import duckdb as duckdb_db


def run_exact_query(sql: str) -> dict:
    """Run exact query and measure time"""
    start = time.perf_counter()
    result = duckdb_db.execute_query(sql)
    elapsed = time.perf_counter() - start

    return {
        "time": elapsed,
        "rows": result["rows"],
        "columns": result["columns"],
    }


def run_approximate_query(sql: str, mode: str = "balanced") -> dict:
    """Run approximate query with adaptive sampling"""
    parsed = parse_analytical_query(sql)
    result = run_runtime_sampling(parsed, "duckdb", mode)

    return result


def compare_results(exact: dict, approx: dict, query_name: str):
    """Compare and display results"""
    print(f"\n{'='*80}")
    print(f"Query: {query_name}")
    print(f"{'='*80}")

    # Display exact results
    print("\n[EXACT EXECUTION]")
    print(f"  Time: {exact['time']:.3f}s")
    print(f"  Rows: {len(exact['rows'])}")
    if len(exact['rows']) > 0 and len(exact['rows']) <= 10:
        print(f"  Results:")
        for row in exact['rows']:
            print(f"    {row}")

    # Display approximate results
    print("\n[APPROXIMATE EXECUTION]")
    print(f"  Time: {approx['time']:.3f}s")
    print(f"  Speedup: {exact['time'] / approx['time']:.2f}x")
    print(f"  Mode: {approx.get('mode_profile', 'N/A')}")
    print(f"  Complexity: {approx.get('query_complexity', 'N/A')}")
    print(f"  Sample Rate: {approx.get('sample_rate', 0) * 100:.1f}%")
    print(f"  Iterations: {len(approx.get('iterations', []))}")
    print(f"  Confidence: {approx.get('confidence', 0):.1f}%")
    print(f"  Stop Reason: {approx.get('stop_reason', 'N/A')}")

    # Calculate accuracy
    if len(exact['rows']) == len(approx['rows']) and len(exact['rows']) > 0:
        print("\n[ACCURACY COMPARISON]")

        # Compare aggregate values
        exact_cols = exact['columns']
        approx_cols = approx['columns']

        for i, row_exact in enumerate(exact['rows']):
            row_approx = approx['rows'][i]

            print(f"\n  Row {i+1}:")
            for j, col in enumerate(exact_cols):
                exact_val = row_exact[j]
                approx_val = row_approx[j]

                # Calculate relative error for numeric columns
                if isinstance(exact_val, (int, float)) and isinstance(approx_val, (int, float)):
                    if exact_val != 0:
                        rel_error = abs(exact_val - approx_val) / abs(exact_val) * 100
                        print(f"    {col}: {exact_val:,.0f} vs {approx_val:,.0f} (error: {rel_error:.2f}%)")
                    else:
                        print(f"    {col}: {exact_val} vs {approx_val}")
                else:
                    print(f"    {col}: {exact_val} vs {approx_val}")
    else:
        print(f"\n[ACCURACY] Cannot compare - different row counts (exact: {len(exact['rows'])}, approx: {len(approx['rows'])})")

    print(f"\n{'='*80}\n")


# TPC-H Queries with JOINs
TPCH_QUERIES = {
    "Q3: Revenue by Customer Segment (2-way JOIN)": """
        SELECT c.c_mktsegment, SUM(l.l_extendedprice * (1 - l.l_discount)) as revenue
        FROM lineitem l
        INNER JOIN orders o ON l.l_orderkey = o.o_orderkey
        INNER JOIN customer c ON o.o_custkey = c.c_custkey
        WHERE l.l_shipdate >= '1995-01-01' AND l.l_shipdate < '1996-01-01'
        GROUP BY c.c_mktsegment
    """,

    "Q5: Revenue by Nation (3-way star schema JOIN)": """
        SELECT n.n_name, SUM(l.l_extendedprice * (1 - l.l_discount)) as revenue
        FROM lineitem l
        INNER JOIN orders o ON l.l_orderkey = o.o_orderkey
        INNER JOIN customer c ON o.o_custkey = c.c_custkey
        INNER JOIN nation n ON c.c_nationkey = n.n_nationkey
        WHERE l.l_shipdate >= '1995-01-01' AND l.l_shipdate < '1996-01-01'
        GROUP BY n.n_name
    """,

    "Q10: Customer Revenue with Returns (3-way JOIN + filter)": """
        SELECT c.c_name, c.c_nationkey, SUM(l.l_extendedprice * (1 - l.l_discount)) as revenue
        FROM lineitem l
        INNER JOIN orders o ON l.l_orderkey = o.o_orderkey
        INNER JOIN customer c ON o.o_custkey = c.c_custkey
        WHERE l.l_returnflag = 'R'
        GROUP BY c.c_name, c.c_nationkey
    """,

    "Q12: Shipping Modes Analysis (2-way JOIN)": """
        SELECT l.l_shipmode, COUNT(*) as order_count
        FROM lineitem l
        INNER JOIN orders o ON l.l_orderkey = o.o_orderkey
        WHERE l.l_shipdate >= '1995-01-01' AND l.l_shipdate < '1996-01-01'
        GROUP BY l.l_shipmode
    """,

    "Q18: Large Orders (2-way JOIN with aggregation)": """
        SELECT c.c_name, COUNT(o.o_orderkey) as order_count, SUM(l.l_quantity) as total_quantity
        FROM lineitem l
        INNER JOIN orders o ON l.l_orderkey = o.o_orderkey
        INNER JOIN customer c ON o.o_custkey = c.c_custkey
        WHERE l.l_shipdate >= '1995-01-01'
        GROUP BY c.c_name
    """,
}


def main():
    print("\n" + "="*80)
    print("TPC-H Approximate JOIN Testing")
    print("="*80)
    print(f"\nDatabase: DuckDB with TPC-H SF=0.1")
    print(f"Tables: lineitem (60.6M rows), orders (150K), customer (15K), nation (25)")
    print(f"Mode: balanced (adaptive sampling, 4% convergence threshold, 1.5s budget)")
    print("="*80)

    # Run each query
    for query_name, sql in TPCH_QUERIES.items():
        try:
            print(f"\nRunning: {query_name}...")

            # Run exact
            print("  [1/2] Exact execution...")
            exact_result = run_exact_query(sql)

            # Run approximate
            print("  [2/2] Approximate execution...")
            approx_result = run_approximate_query(sql, mode="balanced")

            # Compare
            compare_results(exact_result, approx_result, query_name)

        except Exception as e:
            print(f"\n❌ ERROR in {query_name}:")
            print(f"   {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            print()

    print("\n" + "="*80)
    print("Testing Complete")
    print("="*80)


if __name__ == "__main__":
    main()
