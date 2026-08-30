"""
Tests for the JOIN safeguards in runtime_sampling.run_runtime_sampling.

The known failure these lock in: a selective JOIN sampled at 1-2% returns zero
rows (or a shrinking subset of GROUP BY groups), and the adaptive loop happily
declared that "converged". Execution is stubbed so the test drives the exact
sequence of partial results the loop must react to.
"""

from __future__ import annotations

import pytest

from backend.core import runtime_sampling as rs
from backend.core.parser import parse_analytical_query


# A LEFT join is deliberately NOT "CI-defensible", so run_runtime_sampling keeps
# it on the group-completeness heuristic path these tests exercise (a defensible
# INNER equi-join would route through the confidence-interval path instead --
# see test_ci_stop.py).
JOIN_SQL = (
    "SELECT c.c_mktsegment, COUNT(*) AS cnt "
    "FROM customer c LEFT JOIN orders o ON c.c_custkey = o.o_custkey "
    "LEFT JOIN lineitem l ON o.o_orderkey = l.l_orderkey "
    "GROUP BY c.c_mktsegment"
)

FULL_GROUPS = ["AUTOMOBILE", "BUILDING", "FURNITURE", "HOUSEHOLD", "MACHINERY"]


def _result_map(groups):
    return {
        str((g,)): {"c_mktsegment": g, "cnt": 1000 + i}
        for i, g in enumerate(groups)
    }


def _payload_for(result_map):
    return {
        "columns": ["c_mktsegment", "cnt"],
        "rows": [[v["c_mktsegment"], v["cnt"]] for v in result_map.values()],
        "result_map": result_map,
    }


def _make_stub(sequence):
    """Return an execute_stratified_join_sample stub yielding `sequence` maps."""
    calls = {"fractions": []}

    def stub(parsed, source, sample_fraction):
        calls["fractions"].append(round(sample_fraction, 4))
        index = min(len(calls["fractions"]) - 1, len(sequence) - 1)
        return _payload_for(sequence[index]), 0.01, f"-- sample {sample_fraction}"

    return stub, calls


def _make_growing_stub(step=50):
    """A stub whose GROUP BY set never stabilises: call k returns k*step groups."""
    calls = {"fractions": []}

    def stub(parsed, source, sample_fraction):
        calls["fractions"].append(round(sample_fraction, 4))
        k = len(calls["fractions"])
        result_map = _result_map([f"CUST_{i}" for i in range(k * step)])
        return _payload_for(result_map), 0.01, f"-- sample {sample_fraction}"

    return stub, calls


def test_join_progression_floor_is_not_clobbered_by_complexity_preset(monkeypatch):
    """
    JOIN_SQL joins three tables (two JoinSpecs) -> the 7% floor applies. Before
    the fix the "complex" complexity preset overwrote the progression with
    [0.02, 0.05, ...] and the first sample went out at 2%.
    """
    parsed = parse_analytical_query(JOIN_SQL)
    assert len(parsed.joins) == 2

    sequence = [_result_map(FULL_GROUPS)]
    stub, calls = _make_stub(sequence)
    monkeypatch.setattr(rs, "execute_stratified_join_sample", stub)

    rs.run_runtime_sampling(parsed, "duckdb", "balanced")

    assert calls["fractions"], "join sampling was never invoked"
    assert min(calls["fractions"]) >= 0.07 - 1e-9, calls["fractions"]


def test_incomplete_groups_block_convergence_and_force_escalation(monkeypatch):
    parsed = parse_analytical_query(JOIN_SQL)

    # First two looks miss groups, third look is complete and stable.
    sequence = [
        _result_map([]),                       # empty
        _result_map(FULL_GROUPS[:2]),          # partial
        _result_map(FULL_GROUPS),              # complete
        _result_map(FULL_GROUPS),              # stable repeat
    ]
    stub, calls = _make_stub(sequence)
    monkeypatch.setattr(rs, "execute_stratified_join_sample", stub)

    result = rs.run_runtime_sampling(parsed, "duckdb", "balanced")

    # It must not have stopped while groups were missing.
    assert result["stop_reason"] != "converged" or result["groups_returned"] == len(FULL_GROUPS)
    assert result["groups_returned"] == len(FULL_GROUPS)
    assert result["groups_incomplete"] is False
    # The sample fraction must have climbed past the starting floor.
    assert calls["fractions"][-1] > calls["fractions"][0]


def test_high_cardinality_join_gives_up_instead_of_full_scanning(monkeypatch):
    """
    If the group set never stabilises (one row per customer), the loop must
    bail out with stop_reason='groups_incomplete' after a bounded number of
    forced jumps rather than grinding through a dozen samples up to 100%.
    Regression guard for the Q10 pathology (13 iterations, 100% sample).
    """
    parsed = parse_analytical_query(JOIN_SQL)
    stub, calls = _make_growing_stub()
    monkeypatch.setattr(rs, "execute_stratified_join_sample", stub)

    result = rs.run_runtime_sampling(parsed, "duckdb", "balanced")

    assert result["stop_reason"] == "groups_incomplete"
    assert result["groups_incomplete"] is True
    assert result["rows"], "should still return its best-effort partial result"
    assert len(calls["fractions"]) <= 8  # bounded, not a 13-sample grind
    assert any(it["groups_incomplete"] for it in result["iterations"])


def test_stable_complete_join_result_can_converge(monkeypatch):
    parsed = parse_analytical_query(JOIN_SQL)
    sequence = [_result_map(FULL_GROUPS)]  # complete and identical every look
    stub, calls = _make_stub(sequence)
    monkeypatch.setattr(rs, "execute_stratified_join_sample", stub)

    result = rs.run_runtime_sampling(parsed, "duckdb", "balanced")

    assert result["groups_incomplete"] is False
    assert result["groups_returned"] == len(FULL_GROUPS)
    assert result["stop_reason"] in {"converged", "progression_exhausted", "time_budget_exceeded"}
