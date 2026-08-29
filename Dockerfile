# Reproducibility image for the AetherQuery AQP evaluation.
# Builds a clean TPC-H SF1 database with DuckDB's dbgen, runs the unit tests,
# regenerates the smoke results, and runs the end-to-end coverage study.
#
#   docker build -t aetherquery-repro .
#   docker run --rm aetherquery-repro            # runs scripts/reproduce.sh
#   docker run --rm -it aetherquery-repro bash   # poke around

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AETHERQUERY_DUCKDB_PATH=/app/aqp_eval/datasets/tpch_sf1.duckdb

WORKDIR /app

# Backend deps only (no Node; the frontend is not part of the eval).
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt pytest

COPY . .

# Generate the clean SF1 database at build time so `docker run` is offline.
RUN python -c "import duckdb; c=duckdb.connect('aqp_eval/datasets/tpch_sf1.duckdb'); \
c.execute('INSTALL tpch'); c.execute('LOAD tpch'); c.execute('CALL dbgen(sf=1)'); \
print('SF1 rows:', c.execute('SELECT COUNT(*) FROM lineitem').fetchone()[0])"

CMD ["bash", "scripts/reproduce.sh"]
