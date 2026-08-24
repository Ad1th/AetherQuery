"""
Generate TPC-H benchmark data for testing approximate JOINs.
Uses DuckDB's built-in TPC-H extension.
"""

import os
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
from backend.db import duckdb as duckdb_db

def generate_tpch_tables(scale_factor: float = 0.1):
    """
    Generate TPC-H tables at specified scale factor.

    Scale factor 0.1 = ~600K orders, ~150K customers, ~60M lineitem rows
    Scale factor 1.0 = ~6M orders, ~1.5M customers, ~600M lineitem rows
    """
    print(f"Generating TPC-H data at scale factor {scale_factor}...")

    # Install and load TPC-H extension
    print("Loading TPC-H extension...")
    duckdb_db.execute_query("INSTALL tpch")
    duckdb_db.execute_query("LOAD tpch")

    # Generate tables
    print(f"Generating tables (SF={scale_factor})...")
    duckdb_db.execute_query(f"CALL dbgen(sf={scale_factor})")

    # Show table sizes
    print("\nGenerated tables:")
    for table in ['customer', 'orders', 'lineitem', 'part', 'supplier', 'partsupp', 'nation', 'region']:
        try:
            result = duckdb_db.execute_query(f"SELECT COUNT(*) FROM {table}")
            count = result['rows'][0][0]
            print(f"  {table:12s}: {count:>12,} rows")
        except Exception as e:
            print(f"  {table:12s}: Error - {e}")

    print("\nTPC-H data generation complete!")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate TPC-H benchmark data")
    parser.add_argument("--scale", type=float, default=0.1,
                       help="Scale factor (0.1 = 600K orders, 1.0 = 6M orders)")
    args = parser.parse_args()

    generate_tpch_tables(args.scale)
