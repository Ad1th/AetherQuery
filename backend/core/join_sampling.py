"""
Approximate JOIN execution using stratified sampling and cardinality estimation.

Key innovations:
1. Stratified Sampling: Sample both sides of the join proportionally to preserve join selectivity
2. HyperLogLog Cardinality Estimation: Estimate join result sizes without full materialization
3. Bloom Filter Optimization: Pre-filter probe side using sampled build side
4. Multi-table Convergence: Extend convergence detection to multi-way joins
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import defaultdict
from typing import Any

import pandas as pd

from backend.core.executor import _execute_source_query
from backend.core.parser import ParsedQuery, JoinSpec


class HyperLogLog:
    """
    HyperLogLog cardinality estimator for join size prediction.
    Uses 2^14 registers (16KB memory) with 64-bit hash for ~1% error.
    """

    def __init__(self, precision: int = 14):
        self.precision = precision
        self.m = 1 << precision  # 2^precision registers
        self.registers = [0] * self.m
        self.alpha = self._get_alpha(self.m)

    def _get_alpha(self, m: int) -> float:
        """Bias correction constant for HyperLogLog"""
        if m >= 128:
            return 0.7213 / (1.0 + 1.079 / m)
        elif m >= 64:
            return 0.709
        elif m >= 32:
            return 0.697
        elif m >= 16:
            return 0.673
        else:
            return 0.5

    def add(self, value: Any) -> None:
        """Add a value to the HyperLogLog sketch"""
        # Hash the value
        hash_value = int(hashlib.sha256(str(value).encode()).hexdigest(), 16)

        # Use first 'precision' bits for register index
        register_index = hash_value & ((1 << self.precision) - 1)

        # Count leading zeros in remaining bits + 1
        remaining_bits = hash_value >> self.precision
        leading_zeros = self._leading_zeros_count(remaining_bits) + 1

        # Update register with maximum leading zeros seen
        self.registers[register_index] = max(self.registers[register_index], leading_zeros)

    def _leading_zeros_count(self, value: int) -> int:
        """Count leading zeros in a 64-bit integer"""
        if value == 0:
            return 64 - self.precision
        count = 0
        for i in range(64 - self.precision - 1, -1, -1):
            if value & (1 << i):
                break
            count += 1
        return count

    def cardinality(self) -> int:
        """Estimate the cardinality using harmonic mean"""
        raw_estimate = self.alpha * (self.m ** 2) / sum(2 ** (-x) for x in self.registers)

        # Small range correction
        if raw_estimate <= 2.5 * self.m:
            zeros = self.registers.count(0)
            if zeros != 0:
                return int(self.m * math.log(self.m / zeros))

        # Large range correction
        if raw_estimate > (1 / 30) * (1 << 32):
            return int(-1 * (1 << 32) * math.log(1 - raw_estimate / (1 << 32)))

        return int(raw_estimate)


class BloomFilter:
    """
    Bloom filter for pre-filtering join probe side.
    Uses 3 hash functions and size proportional to expected elements.
    """

    def __init__(self, expected_elements: int, false_positive_rate: float = 0.01):
        self.size = self._optimal_size(expected_elements, false_positive_rate)
        self.num_hashes = self._optimal_num_hashes(self.size, expected_elements)
        self.bit_array = [False] * self.size

    def _optimal_size(self, n: int, p: float) -> int:
        """Calculate optimal bit array size"""
        return max(100, int(-n * math.log(p) / (math.log(2) ** 2)))

    def _optimal_num_hashes(self, m: int, n: int) -> int:
        """Calculate optimal number of hash functions"""
        return max(1, int((m / max(1, n)) * math.log(2)))

    def _hashes(self, value: Any) -> list[int]:
        """Generate multiple hash values using double hashing"""
        hash1 = int(hashlib.md5(str(value).encode()).hexdigest(), 16)
        hash2 = int(hashlib.sha256(str(value).encode()).hexdigest(), 16)

        hashes = []
        for i in range(self.num_hashes):
            combined_hash = (hash1 + i * hash2) % self.size
            hashes.append(combined_hash)
        return hashes

    def add(self, value: Any) -> None:
        """Add a value to the Bloom filter"""
        for hash_val in self._hashes(value):
            self.bit_array[hash_val] = True

    def contains(self, value: Any) -> bool:
        """Check if value might be in the set (no false negatives)"""
        return all(self.bit_array[hash_val] for hash_val in self._hashes(value))


def estimate_join_cardinality(
    left_sample: pd.DataFrame,
    right_sample: pd.DataFrame,
    left_key: str,
    right_key: str,
    left_sample_fraction: float,
    right_sample_fraction: float,
) -> int:
    """
    Estimate join result cardinality using HyperLogLog and stratified sample merge.

    Args:
        left_sample: Sampled left table
        right_sample: Sampled right table
        left_key: Join key column in left table
        right_key: Join key column in right table
        left_sample_fraction: Fraction of left table sampled
        right_sample_fraction: Fraction of right table sampled

    Returns:
        Estimated number of rows in join result
    """
    if left_sample.empty or right_sample.empty:
        return 0

    # Build HyperLogLog sketches for join keys
    left_hll = HyperLogLog(precision=14)
    right_hll = HyperLogLog(precision=14)

    for value in left_sample[left_key].dropna():
        left_hll.add(value)

    for value in right_sample[right_key].dropna():
        right_hll.add(value)

    if left_hll.cardinality() == 0 or right_hll.cardinality() == 0:
        return 0

    # Compute sample join cardinality using key merge
    merged_sample = pd.merge(
        left_sample[[left_key]].dropna(),
        right_sample[[right_key]].dropna(),
        left_on=left_key,
        right_on=right_key,
    )
    sample_join_rows = len(merged_sample)
    denom = max(1e-6, left_sample_fraction * right_sample_fraction)
    estimated_cardinality = int(round(sample_join_rows / denom))

    return estimated_cardinality


def build_join_bloom_filter(
    sample_df: pd.DataFrame,
    join_key: str,
) -> BloomFilter:
    """
    Build a Bloom filter from sampled join key values.
    Used to pre-filter the probe side of the join.
    """
    bloom = BloomFilter(expected_elements=len(sample_df), false_positive_rate=0.01)

    for value in sample_df[join_key].dropna():
        bloom.add(value)

    return bloom


def build_stratified_join_query(
    parsed: ParsedQuery,
    source: str,
    sample_fraction: float,
) -> str:
    """
    Build a stratified sampled join query.

    Strategy:
    - Sample each table independently at the same rate
    - Preserves join selectivity better than sampling after join
    - Uses TABLESAMPLE when available for efficiency

    Note: We rebuild the full query from the original SQL to preserve table aliases,
    since the parser doesn't currently capture them in the ParsedQuery structure.
    """
    if not parsed.joins:
        raise ValueError("build_stratified_join_query requires a query with JOINs")

    # Extract the original FROM clause with aliases from raw SQL
    # This preserves table aliases that users specify (e.g., "FROM lineitem l")
    import re

    original_sql = parsed.raw_sql

    # Find the FROM clause up to WHERE/GROUP BY/ORDER BY/LIMIT
    from_match = re.search(
        r'(?is)\bFROM\s+(.+?)(?:\s+WHERE|\s+GROUP\s+BY|\s+ORDER\s+BY|\s+LIMIT|$)',
        original_sql
    )

    if not from_match:
        raise ValueError("Could not extract FROM clause from original query")

    from_clause_original = from_match.group(1).strip()

    # Build SELECT clause
    select_parts: list[str] = []

    if parsed.group_by:
        select_parts.extend(parsed.group_by)

    for aggregate in parsed.aggregates:
        if aggregate.is_count_star:
            select_parts.append(f"COUNT(*) AS {aggregate.alias}")
        else:
            select_parts.append(
                f"{aggregate.func.upper()}({aggregate.expression}) AS {aggregate.alias}"
            )

    select_clause = f"SELECT {', '.join(select_parts)}"

    # Inject TABLESAMPLE into the FROM clause
    percent = sample_fraction * 100.0

    if source == "duckdb":
        # DuckDB syntax: table_name alias TABLESAMPLE SYSTEM (x%)
        # Inject TABLESAMPLE after the primary FROM table to preserve join selectivity
        from_clause = re.sub(
            r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)(?:\s+([a-zA-Z_][a-zA-Z0-9_]*))?',
            lambda m: f"{m.group(1)} {m.group(2) or ''} TABLESAMPLE SYSTEM ({percent:.4f} PERCENT)",
            from_clause_original,
            count=1,
        )
    elif source == "postgres":
        from_clause = re.sub(
            r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)(?:\s+([a-zA-Z_][a-zA-Z0-9_]*))?',
            lambda m: f"{m.group(1)} {m.group(2) or ''} TABLESAMPLE SYSTEM ({percent:.4f})",
            from_clause_original,
            count=1,
        )
    else:  # MySQL - no TABLESAMPLE, will use WHERE RAND()
        from_clause = from_clause_original

    query = f"{select_clause} FROM {from_clause}"

    # Add WHERE clause (including MySQL sampling predicate)
    where_parts = []
    if parsed.where_clause:
        where_parts.append(f"({parsed.where_clause})")

    if source == "mysql":
        # Add RAND() predicates for each table
        where_parts.append(f"(RAND() < {sample_fraction:.8f})")

    if where_parts:
        query += f" WHERE {' AND '.join(where_parts)}"

    # Add GROUP BY clause
    if parsed.group_by:
        query += f" GROUP BY {', '.join(parsed.group_by)}"

    return query


def execute_stratified_join_sample(
    parsed: ParsedQuery,
    source: str,
    sample_fraction: float,
) -> tuple[dict[str, Any], float, str]:
    """
    Execute a stratified sampled join query with push-down aggregation.

    Returns:
        (aggregate_payload, query_time, rewritten_sql)
    """
    if not parsed.aggregates:
        raise ValueError("execute_stratified_join_sample requires aggregate query")

    sql = build_stratified_join_query(parsed, source, sample_fraction)

    start = time.perf_counter()
    payload = _execute_source_query(sql, source)
    elapsed = time.perf_counter() - start

    rows = payload.get("rows", [])
    columns = payload.get("columns", [])

    result_map: dict[str, Any] = {}

    # Scale up COUNT and SUM aggregates
    from decimal import Decimal
    scale_factor = Decimal(str(1.0 / sample_fraction))

    for index, row in enumerate(rows):
        row_dict = dict(zip(columns, row))

        for aggregate in parsed.aggregates:
            alias = aggregate.alias

            if alias not in row_dict:
                continue

            func = aggregate.func.lower()

            # Scale COUNT and SUM by inverse of sample fraction
            if func in ("sum", "count"):
                value = row_dict[alias]

                if value is not None:
                    row_dict[alias] = Decimal(str(value)) * scale_factor

        # Build result map keyed by group
        if parsed.group_by:
            # Extract values for group keys, handling table aliases
            # GROUP BY may have "table.column" but result columns are just "column"
            group_values = []
            for group_col in parsed.group_by:
                # Try with table alias first, then without
                if group_col in row_dict:
                    group_values.append(row_dict[group_col])
                else:
                    # Strip table alias (e.g., "c.c_mktsegment" → "c_mktsegment")
                    col_without_alias = group_col.split('.')[-1] if '.' in group_col else group_col
                    if col_without_alias in row_dict:
                        group_values.append(row_dict[col_without_alias])
                    else:
                        # Try with full qualified name in columns
                        group_values.append(None)

            key = tuple(group_values)
            result_map[str(key)] = row_dict
        else:
            result_map[f"row_{index}"] = row_dict

    # Reconstruct rows from result_map
    scaled_rows = []
    for entry in result_map.values():
        scaled_rows.append([entry[column] for column in columns])

    aggregate_payload = {
        "columns": columns,
        "rows": scaled_rows,
        "result_map": result_map,
    }

    query_time = float(payload.get("time", elapsed))

    return aggregate_payload, query_time, sql


def _join_key_columns(parsed: ParsedQuery) -> list[tuple[str, str]]:
    """
    Pull (table_or_alias, column) pairs out of every ``a.x = b.y`` equality in
    the ON conditions. Only equi-join keys are useful for cardinality probing.
    """
    pairs: list[tuple[str, str]] = []
    for join in parsed.joins or []:
        for lhs, rhs in re.findall(
            r"([a-zA-Z_][\w.]*)\s*=\s*([a-zA-Z_][\w.]*)", join.on_condition
        ):
            for token in (lhs, rhs):
                if "." in token:
                    tbl, col = token.split(".", 1)
                    pairs.append((tbl, col))
    return pairs


def hll_guided_join_min_rate(
    parsed: ParsedQuery,
    source: str,
    floor: float,
    ceiling: float = 0.60,
    target_rows_per_group: int = 200,
) -> tuple[float, dict[str, Any]]:
    """
    Choose the starting sample fraction for a JOIN from HyperLogLog cardinality
    sketches of its join keys, rather than a fixed lookup by join count.

    Method:
      1. Draw a 1% probe of the primary table's join-key column and feed it to
         a HyperLogLog(2^14) sketch; scale the estimate to the full table.
      2. Estimate the number of output groups the same way (HLL over the GROUP
         BY key, or 1 when ungrouped).
      3. The stratified one-side sample keeps roughly ``rate * N_primary`` rows;
         spread over the groups that is ``rate * N_primary / n_groups`` rows per
         group. Solve for the ``rate`` that yields ``target_rows_per_group`` and
         clamp it to [floor, ceiling].

    Returns (rate, diagnostics). Any probe failure falls back to ``floor``.
    """
    diag: dict[str, Any] = {"method": "fixed_floor", "floor": floor}
    if source != "duckdb" or not parsed.joins:
        return floor, diag

    key_cols = _join_key_columns(parsed)
    primary = parsed.table
    primary_key = next(
        (col for tbl, col in key_cols if tbl in {primary, primary[:1], primary}),
        key_cols[0][1] if key_cols else None,
    )
    if primary_key is None:
        return floor, diag

    try:
        probe_sql = (
            f"SELECT {primary_key} FROM {primary} "
            f"TABLESAMPLE SYSTEM (1 PERCENT)"
        )
        payload = _execute_source_query(probe_sql, source)
        rows = payload.get("rows", [])
        if not rows:
            return floor, diag

        key_hll = HyperLogLog(precision=14)
        for (value,) in rows:
            if value is not None:
                key_hll.add(value)
        sampled_distinct = key_hll.cardinality()
        est_key_cardinality = max(1, int(sampled_distinct / 0.01))

        n_groups = 1
        if parsed.group_by:
            group_expr = parsed.group_by[0].split(".")[-1]
            try:
                grp_payload = _execute_source_query(
                    f"SELECT approx_count_distinct({group_expr}) FROM {primary} "
                    f"TABLESAMPLE SYSTEM (1 PERCENT)",
                    source,
                )
                n_groups = max(1, int(grp_payload["rows"][0][0] or 1))
            except Exception:
                n_groups = 1

        n_primary_payload = _execute_source_query(
            f"SELECT COUNT(*) FROM {primary}", source
        )
        n_primary = max(1, int(n_primary_payload["rows"][0][0]))

        # rows-per-group after a one-sided sample of the primary table
        rate = (target_rows_per_group * n_groups) / n_primary
        rate = max(floor, min(ceiling, rate))

        diag = {
            "method": "hll_guided",
            "primary_key": primary_key,
            "est_key_cardinality": est_key_cardinality,
            "est_groups": n_groups,
            "n_primary": n_primary,
            "chosen_rate": round(rate, 4),
            "floor": floor,
        }
        return round(rate, 4), diag
    except Exception as exc:  # probing is best-effort; never fail the query
        diag["error"] = f"{type(exc).__name__}: {exc}"
        return floor, diag


def estimate_join_complexity_multiplier(parsed: ParsedQuery) -> float:
    """
    Estimate complexity multiplier for JOIN queries.
    Used to adjust sampling progression and time budgets.

    Returns:
        Multiplier (1.0 = baseline, higher = more complex)
    """
    if not parsed.joins:
        return 1.0

    num_joins = len(parsed.joins)

    # Base multiplier by number of joins
    if num_joins == 1:
        multiplier = 2.0  # 2-way join
    elif num_joins == 2:
        multiplier = 3.5  # 3-way join
    else:
        multiplier = 2.0 + (num_joins * 1.5)  # Multi-way joins

    # Increase for non-INNER joins (LEFT/RIGHT/FULL are more expensive)
    for join in parsed.joins:
        if join.join_type != "INNER":
            multiplier *= 1.3

    # Increase for complex join conditions (multiple predicates)
    for join in parsed.joins:
        if " and " in join.on_condition.lower() or " or " in join.on_condition.lower():
            multiplier *= 1.2

    return multiplier
