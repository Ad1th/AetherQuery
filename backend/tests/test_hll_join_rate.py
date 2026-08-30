"""
Tests for HyperLogLog-guided JOIN sample-rate selection
(join_sampling.hll_guided_join_min_rate).

Execution is stubbed so the test controls the probe results and asserts the
rate the sketch-driven formula returns.
"""

from __future__ import annotations

import pytest

from backend.core import join_sampling as js
from backend.core.parser import parse_analytical_query

SQL = (
    "SELECT c.c_mktsegment, COUNT(*) AS c "
    "FROM customer c JOIN orders o ON c.c_custkey = o.o_custkey "
    "GROUP BY c.c_mktsegment"
)


def test_join_key_columns_extracts_equijoin_pairs():
    parsed = parse_analytical_query(SQL)
    pairs = js._join_key_columns(parsed)
    assert ("c", "c_custkey") in pairs
    assert ("o", "o_custkey") in pairs


def test_non_duckdb_source_returns_floor_untouched():
    parsed = parse_analytical_query(SQL)
    rate, diag = js.hll_guided_join_min_rate(parsed, "postgres", floor=0.07)
    assert rate == 0.07
    assert diag["method"] == "fixed_floor"


def test_hll_guided_rate_scales_with_group_count(monkeypatch):
    parsed = parse_analytical_query(SQL)

    # 100 distinct keys in a 1% probe -> ~10k table cardinality; 50 groups;
    # 1,000,000 primary rows. target_rows_per_group=200 -> 200*50/1e6 = 0.01,
    # which is below the floor, so the floor wins but the method is HLL.
    def _exec(sql, source):
        if "approx_count_distinct" in sql:
            return {"columns": ["g"], "rows": [[50]]}
        if "COUNT(*) FROM customer" in sql:
            return {"columns": ["n"], "rows": [[1_000_000]]}
        # the key probe
        return {"columns": ["c_custkey"], "rows": [[i] for i in range(100)]}

    monkeypatch.setattr(js, "_execute_source_query", _exec)
    rate, diag = js.hll_guided_join_min_rate(parsed, "duckdb", floor=0.05)
    assert diag["method"] == "hll_guided"
    assert diag["est_groups"] == 50
    assert rate >= 0.05  # floor respected


def test_hll_guided_rate_rises_for_many_groups(monkeypatch):
    parsed = parse_analytical_query(SQL)

    def _exec(sql, source):
        if "approx_count_distinct" in sql:
            return {"columns": ["g"], "rows": [[20_000]]}
        if "COUNT(*) FROM customer" in sql:
            return {"columns": ["n"], "rows": [[1_000_000]]}
        return {"columns": ["c_custkey"], "rows": [[i] for i in range(500)]}

    monkeypatch.setattr(js, "_execute_source_query", _exec)
    rate, diag = js.hll_guided_join_min_rate(
        parsed, "duckdb", floor=0.05, target_rows_per_group=200
    )
    # 200 * 20000 / 1e6 = 4.0 -> clamped to the ceiling, not the floor
    assert rate > 0.05
    assert rate <= 0.60


def test_probe_failure_falls_back_to_floor(monkeypatch):
    parsed = parse_analytical_query(SQL)

    def _boom(sql, source):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(js, "_execute_source_query", _boom)
    rate, diag = js.hll_guided_join_min_rate(parsed, "duckdb", floor=0.08)
    assert rate == 0.08
    assert "error" in diag
