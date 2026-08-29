"""
Tests for confidence-interval-based stopping in runtime_sampling, and the
sufficient-statistics fetch that feeds it.

Execution is stubbed: each test hands `_execute_source_query` a fixed set of
pushed-down sufficient statistics so the assertions are about the controller's
decisions, not a database's RNG.
"""

from __future__ import annotations

import pytest

from backend.core import runtime_sampling as rs
from backend.core import sufficient_stats as ss
from backend.core.parser import parse_analytical_query


# ---------------------------------------------------------------------------
# sufficient_stats.evaluate_sample_accuracy
# ---------------------------------------------------------------------------

def _stub_exec(mapping):
    """mapping: dict[sql-substring -> payload]. Longest matching key wins."""
    def _exec(sql, source):
        best = None
        for needle, payload in mapping.items():
            if needle in sql and (best is None or len(needle) > len(best[0])):
                best = (needle, payload)
        if best is None:
            raise AssertionError(f"no stub for SQL: {sql}")
        return best[1]
    return _exec


def test_ungrouped_count_is_reported_exact(monkeypatch):
    parsed = parse_analytical_query("SELECT COUNT(*) AS cnt FROM lineitem")
    monkeypatch.setattr(
        ss, "_execute_source_query",
        _stub_exec({
            "COUNT(*) FROM lineitem": {"columns": ["c"], "rows": [[6_000_000]]},
            "TABLESAMPLE": {
                "columns": ["__aqp_n_bucket", "__aqp_n_domain"],
                "rows": [[60_000, 60_000]],
            },
        }),
    )
    ss._POPULATION_CACHE.clear()
    es, met, detail = ss.evaluate_sample_accuracy(
        parsed, "duckdb", 0.01, target_relative_error=0.05
    )
    assert met is True
    # domain_is_universe -> COUNT(*) == N with a zero-width interval
    assert detail["max_relative_half_width"] == 0.0
    assert list(detail["result_map"].values())[0]["cnt"] == pytest.approx(6_000_000)


def test_group_columns_are_aliased_so_qualified_keys_survive():
    parsed = parse_analytical_query(
        "SELECT c.c_mktsegment, COUNT(*) AS c FROM customer c "
        "JOIN orders o ON c.c_custkey = o.o_custkey GROUP BY c.c_mktsegment"
    )
    sql = ss.build_sufficient_stats_sql(parsed, "duckdb", 0.05)
    # the qualified group expr must be re-aliased, not selected bare
    assert "c.c_mktsegment AS __aqp_grp_0" in sql
    assert "TABLESAMPLE SYSTEM" in sql.split("JOIN")[0]  # sample on the primary only


def test_join_ci_is_defensible_gate():
    inner = parse_analytical_query(
        "SELECT n.n_name, COUNT(*) AS c FROM lineitem l "
        "JOIN nation n ON l.l_suppkey = n.n_nationkey GROUP BY n.n_name"
    )
    left = parse_analytical_query(
        "SELECT n.n_name, COUNT(*) AS c FROM lineitem l "
        "LEFT JOIN nation n ON l.l_suppkey = n.n_nationkey GROUP BY n.n_name"
    )
    plain = parse_analytical_query("SELECT COUNT(*) AS c FROM lineitem")
    assert ss.join_ci_is_defensible(inner) is True
    assert ss.join_ci_is_defensible(left) is False
    assert ss.join_ci_is_defensible(plain) is False


def test_design_effect_widens_sum_intervals_but_not_count(monkeypatch):
    parsed = parse_analytical_query(
        "SELECT l_returnflag, COUNT(*) AS c, SUM(l_extendedprice) AS s "
        "FROM lineitem GROUP BY l_returnflag"
    )
    ss._POPULATION_CACHE.clear()
    payload = {
        "columns": [
            "__aqp_grp_0", "__aqp_n_bucket", "__aqp_n_domain",
            "__aqp_sum__s", "__aqp_sumxx__s", "__aqp_sumxxx__s",
            "__aqp_var__s", "__aqp_min__s", "__aqp_max__s", "__aqp_skew__s",
        ],
        "rows": [["A", 50_000, 50_000, 1.5e9, 5.0e13, 2e18, 4.0e8, 900.0, 100000.0, 0.4]],
    }
    monkeypatch.setattr(ss, "_execute_source_query", _stub_exec({
        "COUNT(*) FROM lineitem": {"columns": ["c"], "rows": [[6_000_000]]},
        "TABLESAMPLE": payload,
    }))

    _, _, d_no = ss.evaluate_sample_accuracy(parsed, "duckdb", 0.01, design_effect=1.0)
    _, _, d_deff = ss.evaluate_sample_accuracy(parsed, "duckdb", 0.01, design_effect=1.5)

    by_no = {e["alias"]: e for e in d_no["ci"]["estimates"]}
    by_deff = {e["alias"]: e for e in d_deff["ci"]["estimates"]}
    assert by_deff["s"]["half_width"] > by_no["s"]["half_width"]
    assert by_deff["c"]["half_width"] == pytest.approx(by_no["c"]["half_width"])


def test_evaluate_sample_accuracy_handles_empty_sample(monkeypatch):
    parsed = parse_analytical_query(
        "SELECT l_returnflag, SUM(l_extendedprice) AS s FROM lineitem GROUP BY l_returnflag"
    )
    ss._POPULATION_CACHE.clear()
    monkeypatch.setattr(ss, "_execute_source_query", _stub_exec({
        "COUNT(*) FROM lineitem": {"columns": ["c"], "rows": [[6_000_000]]},
        "TABLESAMPLE": {"columns": [
            "__aqp_grp_0", "__aqp_n_bucket", "__aqp_n_domain",
            "__aqp_sum__s", "__aqp_sumxx__s", "__aqp_sumxxx__s",
            "__aqp_var__s", "__aqp_min__s", "__aqp_max__s", "__aqp_skew__s",
        ], "rows": []},
    }))
    es, met, detail = ss.evaluate_sample_accuracy(parsed, "duckdb", 0.001)
    assert met is False
    assert detail["rows"] == []
    assert detail["max_relative_half_width"] is None


def test_join_ci_falls_back_when_block_query_fails(monkeypatch):
    parsed = parse_analytical_query(
        "SELECT c.c_mktsegment, COUNT(*) AS c FROM customer c "
        "JOIN orders o ON c.c_custkey = o.o_custkey GROUP BY c.c_mktsegment"
    )
    ss._POPULATION_CACHE.clear()
    calls = {"n": 0}

    def _exec(sql, source):
        if "COUNT(*) FROM customer" in sql and "rowid" not in sql:
            return {"columns": ["n"], "rows": [[150_000]]}
        if "rowid" in sql:
            raise RuntimeError("Binder Error: rowid not available on this table")
        # the naive fallback path (build_sufficient_stats_sql join branch)
        calls["n"] += 1
        return {"columns": [
            "c.c_mktsegment", "__aqp_grp_0", "__aqp_n_bucket", "__aqp_n_domain",
        ], "rows": [["BUILDING", "BUILDING", 5000, 5000],
                    ["AUTOMOBILE", "AUTOMOBILE", 4000, 4000]]}

    monkeypatch.setattr(ss, "_execute_source_query", _exec)
    es, met, detail = ss.evaluate_join_sample_accuracy(parsed, "duckdb", 0.05)
    # did not raise; produced a (naive) estimate grid instead
    assert calls["n"] >= 1
    assert set(detail["result_map"]) != set()


def test_non_duckdb_join_ci_delegates_to_single_table_path(monkeypatch):
    parsed = parse_analytical_query(
        "SELECT c.c_mktsegment, COUNT(*) AS c FROM customer c "
        "JOIN orders o ON c.c_custkey = o.o_custkey GROUP BY c.c_mktsegment"
    )
    ss._POPULATION_CACHE.clear()

    def _exec(sql, source):
        assert "rowid" not in sql, "postgres path must not reference rowid"
        if "COUNT(*) FROM customer" in sql:
            return {"columns": ["n"], "rows": [[150_000]]}
        return {"columns": [
            "c.c_mktsegment", "__aqp_grp_0", "__aqp_n_bucket", "__aqp_n_domain",
        ], "rows": [["BUILDING", "BUILDING", 5000, 5000]]}

    monkeypatch.setattr(ss, "_execute_source_query", _exec)
    es, met, detail = ss.evaluate_join_sample_accuracy(parsed, "postgres", 0.05)
    assert "result_map" in detail


def test_join_sample_accuracy_uses_cluster_estimator(monkeypatch):
    parsed = parse_analytical_query(
        "SELECT c.c_mktsegment, COUNT(*) AS c "
        "FROM customer c JOIN orders o ON c.c_custkey = o.o_custkey "
        "GROUP BY c.c_mktsegment"
    )
    ss._POPULATION_CACHE.clear()

    # 40 fact blocks sampled, two mktsegments, ~500 joined rows per (block,group)
    block_rows = []
    for blk in range(40):
        for seg in ("BUILDING", "AUTOMOBILE"):
            block_rows.append({
                "__aqp_blk": blk, "__aqp_grp_0": seg,
                "__aqp_n_rows": 500 + (blk % 7) * 20, "__aqp_n_domain": 500 + (blk % 7) * 20,
            })
    cols = list(block_rows[0].keys())

    def _exec(sql, source):
        if "COUNT(*) FROM customer" in sql and "rowid" not in sql:
            return {"columns": ["n"], "rows": [[150_000]]}
        return {"columns": cols, "rows": [[r[c] for c in cols] for r in block_rows]}

    monkeypatch.setattr(ss, "_execute_source_query", _exec)

    es, met, detail = ss.evaluate_join_sample_accuracy(
        parsed, "duckdb", 0.05, target_relative_error=0.10
    )
    methods = {e["method"] for e in detail["ci"]["estimates"]}
    assert methods == {"cluster_clt"}
    assert detail["blocks_sampled"] == 40
    for e in detail["ci"]["estimates"]:
        assert e["ci_low"] < e["estimate"] < e["ci_high"]
        # df from blocks, not rows
        assert e["n_domain"] > 40


def test_grouped_sum_interval_tightens_with_fraction(monkeypatch):
    parsed = parse_analytical_query(
        "SELECT l_returnflag, SUM(l_extendedprice) AS s FROM lineitem GROUP BY l_returnflag"
    )
    ss._POPULATION_CACHE.clear()

    def payload_for(frac):
        n = int(6_000_000 * frac)
        # two groups, moderate spread
        return {
            "columns": [
                "l_returnflag", "__aqp_n_bucket", "__aqp_n_domain",
                "__aqp_sum__s", "__aqp_var__s", "__aqp_min__s", "__aqp_max__s", "__aqp_skew__s",
            ],
            "rows": [
                ["A", n // 2, n // 2, (n // 2) * 30000.0, 4.0e8, 900.0, 100000.0, 0.4],
                ["N", n // 2, n // 2, (n // 2) * 30000.0, 4.0e8, 900.0, 100000.0, 0.4],
            ],
        }

    seen = {}
    def _exec(sql, source):
        if "COUNT(*) FROM lineitem" in sql and "TABLESAMPLE" not in sql:
            return {"columns": ["c"], "rows": [[6_000_000]]}
        frac = float(sql.split("SYSTEM (")[1].split(" PERCENT")[0]) / 100.0
        seen["frac"] = frac
        return payload_for(frac)

    monkeypatch.setattr(ss, "_execute_source_query", _exec)

    _, _, d_small = ss.evaluate_sample_accuracy(parsed, "duckdb", 0.001, target_relative_error=0.05)
    _, _, d_big = ss.evaluate_sample_accuracy(parsed, "duckdb", 0.20, target_relative_error=0.05)
    assert d_big["max_relative_half_width"] < d_small["max_relative_half_width"]


# ---------------------------------------------------------------------------
# runtime_sampling: CI stop path
# ---------------------------------------------------------------------------

def _patch_engine_ci(monkeypatch, sequence):
    """sequence: list of (ci_met, max_rel_hw) the engine 'sees' per iteration."""
    calls = {"fractions": []}

    def fake_eval(parsed, source, sample_fraction, *, coverage_level, target_relative_error):
        calls["fractions"].append(round(sample_fraction, 4))
        i = min(len(calls["fractions"]) - 1, len(sequence) - 1)
        met, hw = sequence[i]
        detail = {
            "columns": ["cnt"],
            "rows": [[123]],
            "result_map": {"row_0": {"cnt": 123}},
            "n_sample": 10_000,
            "sample_query": f"-- {sample_fraction}",
            "query_time": 0.01,
            "max_relative_half_width": hw,
            "unresolved_cells": 0,
            "ci": {"max_relative_half_width": hw},
        }
        return object(), met, detail

    monkeypatch.setattr(rs, "evaluate_sample_accuracy", fake_eval)
    return calls


def test_ci_stop_fires_when_interval_within_target(monkeypatch):
    parsed = parse_analytical_query("SELECT COUNT(*) AS cnt FROM lineitem")
    calls = _patch_engine_ci(monkeypatch, [(False, 0.2), (True, 0.01)])

    result = rs.run_runtime_sampling(parsed, "duckdb", "balanced")

    assert result["stop_reason"] == "ci_within_target"
    assert result["confidence"] == 95.0
    assert result["estimated_error"] == pytest.approx(1.0)  # 0.01 * 100
    assert result["target_relative_error"] is not None
    assert len(calls["fractions"]) == 2


def test_ci_path_escalates_and_never_reports_false_converged(monkeypatch):
    parsed = parse_analytical_query("SELECT COUNT(*) AS cnt FROM lineitem")
    # interval never gets within target
    calls = _patch_engine_ci(monkeypatch, [(False, 0.5)] * 12)

    result = rs.run_runtime_sampling(parsed, "duckdb", "balanced")

    assert result["stop_reason"] in {"time_budget_exceeded", "progression_exhausted"}
    assert result["stop_reason"] != "converged"
    assert result["confidence"] < 95.0
    assert calls["fractions"] == sorted(calls["fractions"])


def test_accuracy_target_sets_a_tighter_error_budget(monkeypatch):
    parsed = parse_analytical_query("SELECT COUNT(*) AS cnt FROM lineitem")
    _patch_engine_ci(monkeypatch, [(True, 0.02)])

    loose = rs.run_runtime_sampling(parsed, "duckdb", "balanced")
    tight = rs.run_runtime_sampling(parsed, "duckdb", "balanced", accuracy_target=99.0)

    assert tight["target_relative_error"] < loose["target_relative_error"]
    assert tight["target_relative_error"] == pytest.approx(0.01)
