"""
Tests for backend.core.error_bounds.

Two layers:
1. The closed-form interval helpers (Hoeffding for COUNT/SUM, CLT for AVG) --
   checked for direction, monotonicity in the sample fraction, and the
   degenerate cases.
2. progressive_refinement_with_bounds -- the adaptive loop. Execution is
   stubbed so the test controls exactly what each sample fraction "sees" and
   can assert the stopping behaviour rather than a database's RNG.
"""

from __future__ import annotations

import math

import pytest

from backend.core import error_bounds as eb
from backend.core.parser import parse_analytical_query


# ---------------------------------------------------------------------------
# closed-form helpers
# ---------------------------------------------------------------------------

class TestClosedFormBounds:
    def test_hoeffding_count_brackets_the_scaled_estimate(self):
        lower, upper = eb.hoeffding_bound_count(1000, 0.1, confidence_level=0.95)
        assert lower < 10_000 < upper
        assert lower > 0

    def test_hoeffding_count_interval_tightens_with_more_data(self):
        _, upper_small = eb.hoeffding_bound_count(100, 0.1)
        _, upper_large = eb.hoeffding_bound_count(10_000, 0.1)
        rel_small = (upper_small - 1_000) / 1_000
        rel_large = (upper_large - 100_000) / 100_000
        assert rel_large < rel_small

    def test_hoeffding_sum_uses_value_range(self):
        narrow = eb.hoeffding_bound_sum(500.0, 0.1, (0.0, 1.0), n_sampled=1000)
        wide = eb.hoeffding_bound_sum(500.0, 0.1, (0.0, 100.0), n_sampled=1000)
        assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])

    def test_clt_avg_falls_back_for_small_samples(self):
        lower, upper = eb.clt_bound_avg(50.0, 10.0, n_sampled=5)
        assert (lower, upper) == (0, 100.0)

    def test_clt_avg_is_symmetric_for_large_samples(self):
        lower, upper = eb.clt_bound_avg(50.0, 10.0, n_sampled=400, confidence_level=0.95)
        assert math.isclose((lower + upper) / 2, 50.0, rel_tol=1e-9)

    def test_relative_error_bound_infinite_on_zero_estimate(self):
        assert eb.compute_relative_error_bound(0.0, -1.0, 1.0) == math.inf

    def test_predict_sample_size_shrinks_as_target_loosens(self):
        tight = eb.predict_required_sample_size(0.01, aggregate_type="count")
        loose = eb.predict_required_sample_size(0.10, aggregate_type="count")
        assert tight >= loose


# ---------------------------------------------------------------------------
# progressive_refinement_with_bounds
# ---------------------------------------------------------------------------

def _payload(columns, rows):
    return {"columns": columns, "rows": rows}


class TestProgressiveRefinement:
    def test_rejects_join_queries(self):
        parsed = parse_analytical_query(
            "SELECT c.c_mktsegment, COUNT(*) AS cnt FROM customer c "
            "JOIN orders o ON c.c_custkey = o.o_custkey GROUP BY c.c_mktsegment"
        )
        with pytest.raises(ValueError):
            eb.progressive_refinement_with_bounds(parsed, "duckdb")

    def test_rejects_non_aggregate_queries(self):
        parsed = parse_analytical_query("SELECT COUNT(*) AS cnt FROM lineitem")
        object.__setattr__(parsed, "aggregates", [])
        with pytest.raises(ValueError):
            eb.progressive_refinement_with_bounds(parsed, "duckdb")

    def test_stops_early_when_bounds_meet_target(self, monkeypatch):
        parsed = parse_analytical_query("SELECT COUNT(*) AS cnt FROM lineitem")

        calls: list[float] = []

        def fake_exec(sql, source):
            # A large sampled count -> a very tight Hoeffding interval on the
            # first look, so the loop should stop immediately.
            calls.append(sql)
            return _payload(["__aqp_n", "cnt"], [[5_000_000, 5_000_000]])

        monkeypatch.setattr("backend.core.executor._execute_source_query", fake_exec)

        result = eb.progressive_refinement_with_bounds(
            parsed, "duckdb", target_error=0.05, max_iterations=6
        )

        assert result["stop_reason"] == "error_bound_met"
        assert result["meets_target"] is True
        assert len(result["iterations"]) == 1
        assert result["max_relative_error"] <= 0.05

    def test_escalates_until_full_scan_when_target_never_met(self, monkeypatch):
        parsed = parse_analytical_query("SELECT COUNT(*) AS cnt FROM lineitem")

        seen_fractions: list[float] = []

        def fake_exec(sql, source):
            # Tiny per-look sample size -> the interval never tightens enough,
            # so the loop must keep climbing the ladder.
            frac = float(sql.split("SYSTEM (")[1].split(" PERCENT")[0]) / 100.0
            seen_fractions.append(round(frac, 4))
            return _payload(["__aqp_n", "cnt"], [[3, 3]])

        monkeypatch.setattr("backend.core.executor._execute_source_query", fake_exec)

        result = eb.progressive_refinement_with_bounds(
            parsed, "duckdb", target_error=0.001, max_iterations=8
        )

        assert result["meets_target"] is False
        assert result["stop_reason"] in {"full_scan", "max_iterations"}
        assert seen_fractions == sorted(seen_fractions)  # monotonically increasing
        assert seen_fractions[-1] <= 1.0

    def test_reports_per_group_bounds_for_grouped_query(self, monkeypatch):
        parsed = parse_analytical_query(
            "SELECT l_returnflag, COUNT(*) AS cnt FROM lineitem GROUP BY l_returnflag"
        )

        def fake_exec(sql, source):
            return _payload(
                ["l_returnflag", "__aqp_n", "cnt"],
                [["A", 1_000_000, 1_000_000], ["R", 900_000, 900_000]],
            )

        monkeypatch.setattr("backend.core.executor._execute_source_query", fake_exec)

        result = eb.progressive_refinement_with_bounds(parsed, "duckdb", target_error=0.05)

        assert set(result["error_bounds"]) == {"('A',)", "('R',)"}
        for group in result["error_bounds"].values():
            assert "cnt" in group
            assert group["cnt"]["lower"] < group["cnt"]["estimate"] < group["cnt"]["upper"]
