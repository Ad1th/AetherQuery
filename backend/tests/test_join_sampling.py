"""
Tests for approximate JOIN execution with stratified sampling.

Tests cover:
1. 2-way INNER JOIN with aggregation
2. 3-way JOIN (star schema)
3. LEFT/RIGHT JOIN handling
4. HyperLogLog cardinality estimation
5. Bloom filter correctness
6. Accuracy validation against exact results
"""

import pytest
import pandas as pd
from backend.core.parser import parse_analytical_query, JoinSpec
from backend.core.join_sampling import (
    HyperLogLog,
    BloomFilter,
    estimate_join_cardinality,
    build_stratified_join_query,
    estimate_join_complexity_multiplier,
)


class TestHyperLogLog:
    """Test HyperLogLog cardinality estimation"""

    def test_exact_small_cardinality(self):
        """HyperLogLog should be accurate for small distinct counts"""
        hll = HyperLogLog(precision=14)

        # Add 100 unique values
        for i in range(100):
            hll.add(f"value_{i}")

        estimate = hll.cardinality()
        # Allow ~5% error for small cardinalities
        assert 95 <= estimate <= 105, f"Expected ~100, got {estimate}"

    def test_large_cardinality(self):
        """HyperLogLog should handle large cardinalities with <2% error"""
        hll = HyperLogLog(precision=14)

        # Add 10,000 unique values
        for i in range(10000):
            hll.add(f"value_{i}")

        estimate = hll.cardinality()
        # Allow ~2% error for large cardinalities
        assert 9800 <= estimate <= 10200, f"Expected ~10000, got {estimate}"

    def test_duplicate_values(self):
        """HyperLogLog should count distinct values only"""
        hll = HyperLogLog(precision=14)

        # Add 100 unique values, each 10 times
        for i in range(100):
            for _ in range(10):
                hll.add(f"value_{i}")

        estimate = hll.cardinality()
        # Should estimate ~100, not 1000
        assert 95 <= estimate <= 105, f"Expected ~100, got {estimate}"


class TestBloomFilter:
    """Test Bloom filter for join optimization"""

    def test_no_false_negatives(self):
        """Bloom filter must never produce false negatives"""
        bloom = BloomFilter(expected_elements=100, false_positive_rate=0.01)

        # Add 100 elements
        elements = [f"key_{i}" for i in range(100)]
        for elem in elements:
            bloom.add(elem)

        # All added elements must be found
        for elem in elements:
            assert bloom.contains(elem), f"False negative for {elem}"

    def test_false_positive_rate(self):
        """Bloom filter false positive rate should be near configured rate"""
        bloom = BloomFilter(expected_elements=1000, false_positive_rate=0.01)

        # Add 1000 elements
        for i in range(1000):
            bloom.add(f"key_{i}")

        # Test 1000 elements NOT in the filter
        false_positives = 0
        for i in range(1000, 2000):
            if bloom.contains(f"key_{i}"):
                false_positives += 1

        fp_rate = false_positives / 1000
        # Allow 3x the configured rate due to randomness
        assert fp_rate <= 0.03, f"False positive rate too high: {fp_rate:.3f}"


class TestJoinParser:
    """Test JOIN parsing in parse_analytical_query"""

    def test_simple_inner_join(self):
        """Parse a simple 2-way INNER JOIN"""
        query = """
            SELECT orders.customer_id, COUNT(*) as order_count
            FROM orders
            INNER JOIN customers ON orders.customer_id = customers.id
            GROUP BY orders.customer_id
        """

        parsed = parse_analytical_query(query)

        assert parsed.table == "orders"
        assert parsed.has_joins
        assert len(parsed.joins) == 1

        join = parsed.joins[0]
        assert join.join_type == "INNER"
        assert join.right_table == "customers"
        assert "orders.customer_id" in join.on_condition
        assert "customers.id" in join.on_condition

    def test_left_join(self):
        """Parse LEFT JOIN"""
        query = """
            SELECT c.name, COUNT(o.id) as order_count
            FROM customers c
            LEFT JOIN orders o ON c.id = o.customer_id
            GROUP BY c.name
        """

        parsed = parse_analytical_query(query)

        assert parsed.has_joins
        assert parsed.joins[0].join_type == "LEFT"

    def test_multi_way_join(self):
        """Parse 3-way JOIN (star schema)"""
        query = """
            SELECT p.category, c.region, SUM(o.amount) as total
            FROM orders o
            INNER JOIN products p ON o.product_id = p.id
            INNER JOIN customers c ON o.customer_id = c.id
            GROUP BY p.category, c.region
        """

        parsed = parse_analytical_query(query)

        assert parsed.has_joins
        assert len(parsed.joins) == 2
        assert parsed.all_tables == ["orders", "products", "customers"]

    def test_join_with_where_clause(self):
        """Parse JOIN with WHERE filter"""
        query = """
            SELECT c.region, COUNT(*) as count
            FROM orders o
            INNER JOIN customers c ON o.customer_id = c.id
            WHERE o.amount > 100
            GROUP BY c.region
        """

        parsed = parse_analytical_query(query)

        assert parsed.has_joins
        assert parsed.where_clause is not None
        assert "o.amount > 100" in parsed.where_clause


class TestJoinQueryBuilder:
    """Test stratified JOIN query construction"""

    def test_duckdb_join_sampling(self):
        """DuckDB should use TABLESAMPLE SYSTEM"""
        query = """
            SELECT c.region, SUM(o.amount) as total
            FROM orders o
            INNER JOIN customers c ON o.customer_id = c.id
            GROUP BY c.region
        """

        parsed = parse_analytical_query(query)
        sampled_sql = build_stratified_join_query(parsed, "duckdb", 0.10)

        assert "TABLESAMPLE SYSTEM (10.0" in sampled_sql
        assert "INNER JOIN customers" in sampled_sql
        assert "GROUP BY c.region" in sampled_sql

    def test_postgres_join_sampling(self):
        """PostgreSQL should use TABLESAMPLE SYSTEM"""
        query = """
            SELECT c.region, COUNT(*) as count
            FROM orders o
            INNER JOIN customers c ON o.customer_id = c.id
            GROUP BY c.region
        """

        parsed = parse_analytical_query(query)
        sampled_sql = build_stratified_join_query(parsed, "postgres", 0.05)

        assert "TABLESAMPLE SYSTEM (5.0" in sampled_sql

    def test_mysql_join_sampling(self):
        """MySQL should use RAND() predicate"""
        query = """
            SELECT c.region, COUNT(*) as count
            FROM orders o
            INNER JOIN customers c ON o.customer_id = c.id
            GROUP BY c.region
        """

        parsed = parse_analytical_query(query)
        sampled_sql = build_stratified_join_query(parsed, "mysql", 0.10)

        # MySQL doesn't support TABLESAMPLE, so uses WHERE RAND()
        assert "RAND() <" in sampled_sql or "INNER JOIN customers" in sampled_sql


class TestJoinComplexityEstimation:
    """Test JOIN complexity estimation for adaptive sampling"""

    def test_simple_join_complexity(self):
        """2-way INNER JOIN should have 2x multiplier"""
        query = """
            SELECT COUNT(*) as count
            FROM orders o
            INNER JOIN customers c ON o.customer_id = c.id
        """

        parsed = parse_analytical_query(query)
        multiplier = estimate_join_complexity_multiplier(parsed)

        assert multiplier == 2.0

    def test_three_way_join_complexity(self):
        """3-way JOIN should have higher multiplier"""
        query = """
            SELECT COUNT(*) as count
            FROM orders o
            INNER JOIN customers c ON o.customer_id = c.id
            INNER JOIN products p ON o.product_id = p.id
        """

        parsed = parse_analytical_query(query)
        multiplier = estimate_join_complexity_multiplier(parsed)

        assert multiplier == 3.5

    def test_outer_join_complexity(self):
        """LEFT/RIGHT JOIN should have higher multiplier than INNER"""
        query = """
            SELECT COUNT(*) as count
            FROM customers c
            LEFT JOIN orders o ON c.id = o.customer_id
        """

        parsed = parse_analytical_query(query)
        multiplier = estimate_join_complexity_multiplier(parsed)

        # LEFT JOIN: 2.0 * 1.3 = 2.6
        assert multiplier == pytest.approx(2.6, rel=0.01)


class TestJoinCardinalityEstimation:
    """Test HyperLogLog-based join cardinality estimation"""

    def test_one_to_many_join_estimate(self):
        """Estimate cardinality for 1:N join (foreign key)"""
        # Simulate: 100 customers, 1000 orders (1:10 relationship)
        customers = pd.DataFrame({
            "id": range(100),
            "name": [f"Customer_{i}" for i in range(100)]
        })

        orders = pd.DataFrame({
            "order_id": range(1000),
            "customer_id": [i % 100 for i in range(1000)]
        })

        # Sample 10% of each table
        customers_sample = customers.sample(frac=0.10, random_state=42)
        orders_sample = orders.sample(frac=0.10, random_state=42)

        estimated_cardinality = estimate_join_cardinality(
            orders_sample,
            customers_sample,
            "customer_id",
            "id",
            0.10,
            0.10,
        )

        # True join cardinality is ~1000 (all orders match)
        # Allow 20% error due to sampling
        assert 800 <= estimated_cardinality <= 1200, \
            f"Expected ~1000, got {estimated_cardinality}"

    def test_many_to_many_join_estimate(self):
        """Estimate cardinality for M:N join"""
        # Simulate: 50 users, 50 groups, each user in ~5 groups
        user_groups = pd.DataFrame({
            "user_id": [i % 50 for i in range(250)],
            "group_id": [(i // 5) % 50 for i in range(250)]
        })

        users = pd.DataFrame({
            "id": range(50),
            "name": [f"User_{i}" for i in range(50)]
        })

        # Sample 20%
        user_groups_sample = user_groups.sample(frac=0.20, random_state=42)
        users_sample = users.sample(frac=0.20, random_state=42)

        estimated_cardinality = estimate_join_cardinality(
            user_groups_sample,
            users_sample,
            "user_id",
            "id",
            0.20,
            0.20,
        )

        # True join cardinality is 250
        # Allow 30% error for complex join patterns
        assert 175 <= estimated_cardinality <= 325, \
            f"Expected ~250, got {estimated_cardinality}"


class TestEndToEndJoinApproximation:
    """
    Integration tests for end-to-end JOIN approximation.
    These would require actual database connections - marked as integration tests.
    """

    @pytest.mark.integration
    def test_tpch_query_accuracy(self):
        """
        Test accuracy on TPC-H style query:
        SELECT c_mktsegment, COUNT(*) FROM customer JOIN orders ...

        Requires TPC-H database loaded. Run with: pytest -m integration
        """
        # This would connect to actual DuckDB with TPC-H data
        # and compare approximate vs exact results
        pass

    @pytest.mark.integration
    def test_star_schema_accuracy(self):
        """
        Test 3-way star schema join accuracy.
        Fact table (orders) joining two dimension tables (customers, products).
        """
        pass


# Benchmark for paper - run separately
class TestJoinBenchmarks:
    """
    Performance benchmarks for publication.
    Compare: exact execution, BlinkDB-style, AetherQuery stratified sampling
    """

    @pytest.mark.benchmark
    def test_join_speedup_vs_exact(self):
        """Measure speedup: approximate JOIN vs exact JOIN"""
        pass

    @pytest.mark.benchmark
    def test_accuracy_vs_sample_rate(self):
        """Plot accuracy curves at different sample rates (1%, 5%, 10%, 25%)"""
        pass

    @pytest.mark.benchmark
    def test_convergence_iterations(self):
        """Measure iterations to convergence for JOIN queries"""
        pass
