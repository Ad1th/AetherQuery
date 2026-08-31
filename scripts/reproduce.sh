#!/usr/bin/env bash
# One-command reproduction of the AetherQuery AQP evaluation.
#
# Assumes: a Python env with backend/requirements.txt + pytest installed, and a
# clean TPC-H DuckDB database. Inside the Docker image both are already set up
# and SF1 lives at aqp_eval/datasets/tpch_sf1.duckdb.
#
#   scripts/reproduce.sh [SCALE_FACTOR]        # default 1
set -euo pipefail

cd "$(dirname "$0")/.."

SF="${1:-1}"
DB="aqp_eval/datasets/tpch_sf${SF}.duckdb"
PY="${PYTHON:-python}"

if [ ! -f "$DB" ]; then
  echo ">>> generating clean TPC-H SF${SF} at ${DB}"
  "$PY" - "$DB" "$SF" <<'PYEOF'
import sys, duckdb
db, sf = sys.argv[1], float(sys.argv[2])
c = duckdb.connect(db); c.execute("INSTALL tpch"); c.execute("LOAD tpch")
c.execute(f"CALL dbgen(sf={sf})")
print("lineitem rows:", c.execute("SELECT COUNT(*) FROM lineitem").fetchone()[0])
PYEOF
fi

export AETHERQUERY_DUCKDB_PATH="$(cd "$(dirname "$DB")" && pwd)/$(basename "$DB")"

echo; echo ">>> unit tests"
"$PY" -m pytest backend/tests aqp_eval/metrics/test_compare.py -q

echo; echo ">>> smoke results (all policies)"
"$PY" -m aqp_eval.runners.run \
  --database "$DB" \
  --output "aqp_eval/results/sf${SF}_smoke.json" \
  --dataset-label "tpch_sf${SF}"

echo; echo ">>> end-to-end coverage / error / speedup study"
"$PY" scripts/run_engine_coverage_study.py \
  --database "$DB" --trials 40 \
  --output "aqp_eval/results/engine_coverage_study_sf${SF}.json"

echo; echo ">>> controller vs fixed sampling vs per-cell stopping"
"$PY" scripts/run_baseline_comparison.py \
  --database "$DB" --trials "${TRIALS:-120}" \
  --output "aqp_eval/results/baseline_comparison_sf${SF}.json"

echo; echo ">>> done. artifacts in aqp_eval/results/"
