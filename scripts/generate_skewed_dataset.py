"""
Generate a heavy-tailed synthetic dataset for the coverage study.

TPC-H's `l_extendedprice` is only mildly right-skewed, so it does not stress the
normal approximation the way real spend / traffic / file-size data does. This
builds a table with the same column names the study uses but a Pareto-tailed
value column, so the exact same queries run and the estimators are tested on a
distribution where a plain CLT interval is known to struggle.

    python scripts/generate_skewed_dataset.py --rows 5000000 --alpha 1.5
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

DEFAULT_OUT = "aqp_eval/datasets/skewed.duckdb"


def generate(out: str, rows: int, alpha: float) -> None:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = duckdb.connect(str(path))

    # Pareto(alpha) tail: x_m / U^(1/alpha), U ~ Uniform(0,1], x_m = 100.
    # Capped so a stray draw does not dominate a whole aggregate.
    con.execute(
        f"""
        CREATE TABLE lineitem AS
        SELECT
            i AS l_orderkey,
            (i % 100000) AS l_suppkey,
            ['A', 'N', 'R'][1 + (i * 2654435761) % 3] AS l_returnflag,
            LEAST(100.0 / power(1e-9 + random(), {1.0 / alpha:.6f}), 5_000_000.0)
                AS l_extendedprice,
            1 + (i % 50) AS l_quantity,
            round(0.10 * random(), 2) AS l_discount
        FROM range({rows}) t(i)
        """
    )
    n, mn, mx, avg, med = con.execute(
        "SELECT COUNT(*), MIN(l_extendedprice), MAX(l_extendedprice), "
        "AVG(l_extendedprice), median(l_extendedprice) FROM lineitem"
    ).fetchone()
    skew = con.execute("SELECT skewness(l_extendedprice) FROM lineitem").fetchone()[0]
    con.close()
    print(f"rows={n:,}  price min/med/avg/max = {mn:.1f}/{med:.1f}/{avg:.1f}/{mx:,.0f}")
    print(f"skewness(l_extendedprice) = {skew:.1f}   (TPC-H is ~1-2)")
    print(f"written: {path}  ({path.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default=DEFAULT_OUT)
    ap.add_argument("--rows", type=int, default=5_000_000)
    ap.add_argument("--alpha", type=float, default=1.5)
    args = ap.parse_args()
    generate(args.output, args.rows, args.alpha)
