"""
What does TABLESAMPLE actually return?

Two properties decide whether the SRS variance formula applies to an
engine-native sample, and both are engine behaviour rather than theory, so both
are measured rather than assumed:

  contiguity        the fraction of sampled rows whose immediate predecessor
                    in physical order was also sampled. Near 1 means whole
                    row groups are being drawn (cluster sampling); near the
                    sampling fraction itself means rows are drawn
                    independently.
  realized fraction n/N against the fraction that was requested. Every
                    estimator in the engine scales by the realized fraction
                    because these differ.

Usage:
    python scripts/probe_tablesample.py \
        --database aqp_eval/datasets/tpch_sf1.duckdb --table lineitem \
        --database aqp_eval/datasets/nyctaxi.duckdb --table trips \
        --output aqp_eval/results/tablesample_probe.json
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRACTIONS = (1.0, 5.0, 10.0)
BLOCK = 2048


def probe(con, table, pct, method):
    ids = [r[0] for r in con.execute(
        f"SELECT rowid AS r FROM {table} TABLESAMPLE {method} ({pct} PERCENT) "
        f"ORDER BY rowid").fetchall()]
    if not ids:
        return None
    s = set(ids)
    adjacent = sum(1 for i in ids if (i - 1) in s)
    starts = sorted({i for i in ids if (i - 1) not in s})
    aligned = sum(1 for x in starts if x % BLOCK == 0)
    return {
        "n": len(ids),
        "contiguous_pct": 100.0 * adjacent / len(ids),
        "runs": len(starts),
        "run_starts_block_aligned": aligned,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", action="append", required=True)
    ap.add_argument("--table", action="append", required=True)
    ap.add_argument("--output", default="aqp_eval/results/tablesample_probe.json")
    a = ap.parse_args()
    if len(a.database) != len(a.table):
        print("give one --table per --database", file=sys.stderr)
        return 2

    import duckdb
    records = []
    print(f"{'table':12} {'method':10} {'req %':>7} {'realized %':>11} "
          f"{'contiguous %':>13} {'runs':>6} {'aligned':>8}")
    print("-" * 74)
    for db, table in zip(a.database, a.table):
        con = duckdb.connect(str(Path(db).resolve()), read_only=True)
        n_pop = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for method in ("SYSTEM", "BERNOULLI"):
            for pct in FRACTIONS:
                r = probe(con, table, pct, method)
                if r is None:
                    continue
                rec = {
                    "database": str(Path(db).resolve()), "table": table,
                    "population": n_pop, "method": method,
                    "requested_pct": pct,
                    "realized_pct": 100.0 * r["n"] / n_pop, **r,
                }
                records.append(rec)
                print(f"{table:12} {method:10} {pct:7.1f} "
                      f"{rec['realized_pct']:11.2f} {r['contiguous_pct']:13.2f} "
                      f"{r['runs']:6d} "
                      f"{r['run_starts_block_aligned']:8d}")
        con.close()

    try:
        sha = subprocess.check_output(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        sha = "unknown"
    try:
        dv = duckdb.__version__
    except Exception:
        dv = "unknown"
    payload = {
        "provenance": {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "engine_git_sha": sha, "duckdb_version": dv,
            "python_version": platform.python_version(),
            "host": platform.platform(),
            "block_size_assumed": BLOCK,
            "experiment": "tablesample_contiguity_and_realized_fraction",
        },
        "records": records,
    }
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
