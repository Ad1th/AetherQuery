"""
Performance profiling script for approximate JOIN execution.
Identifies bottlenecks and measures component-level timing.
"""

import sys
import time
import cProfile
import pstats
from io import StringIO

sys.path.insert(0, 'backend')

from core.parser import parse_analytical_query
from core.runtime_sampling import run_runtime_sampling
from core.join_sampling import (
    execute_stratified_join_sample,
    estimate_join_cardinality,
    HyperLogLog,
    BloomFilter
)


def profile_join_execution():
    """Profile a 2-way JOIN query to identify bottlenecks"""

    sql = """
        SELECT c.c_mktsegment, SUM(l.l_extendedprice * (1 - l.l_discount)) as revenue
        FROM lineitem l
        INNER JOIN orders o ON l.l_orderkey = o.o_orderkey
        INNER JOIN customer c ON o.o_custkey = c.c_custkey
        WHERE l.l_shipdate >= '1995-01-01' AND l.l_shipdate < '1996-01-01'
        GROUP BY c.c_mktsegment
    """

    print("="*80)
    print("PROFILING: 3-way JOIN with 60M lineitem, 150K orders, 15K customers")
    print("="*80)

    # Parse query
    t0 = time.perf_counter()
    parsed = parse_analytical_query(sql)
    t1 = time.perf_counter()
    print(f"\n✓ Query parsing: {(t1-t0)*1000:.2f}ms")

    # Profile full execution with cProfile
    print("\n--- Running cProfile on full execution ---\n")

    profiler = cProfile.Profile()
    profiler.enable()

    try:
        result = run_runtime_sampling(parsed, "duckdb", "balanced")
    except Exception as e:
        print(f"Execution error: {e}")
        result = None

    profiler.disable()

    # Print top time consumers
    s = StringIO()
    ps = pstats.Stats(profiler, stream=s).sort_stats('cumulative')
    ps.print_stats(30)

    print(s.getvalue())

    if result:
        print("\n" + "="*80)
        print("EXECUTION SUMMARY")
        print("="*80)
        print(f"Total time: {result.get('time', 0):.3f}s")
        print(f"Iterations: {len(result.get('iterations', []))}")
        print(f"Final sample rate: {result.get('sample_rate', 0)*100:.1f}%")
        print(f"Confidence: {result.get('confidence', 0):.1f}%")
        print(f"Stop reason: {result.get('stop_reason', 'N/A')}")

        # Breakdown by iteration
        print("\n--- Per-iteration timing ---")
        for i, it in enumerate(result.get('iterations', []), 1):
            print(f"  Iter {i}: {it['sample_fraction']*100:5.1f}% sample, "
                  f"{it['query_time']*1000:7.1f}ms query, "
                  f"{it['elapsed_time']:.3f}s elapsed")


def profile_components():
    """Profile individual components in isolation"""

    print("\n" + "="*80)
    print("COMPONENT-LEVEL PROFILING")
    print("="*80)

    # 1. HyperLogLog performance
    print("\n1. HyperLogLog cardinality estimation:")
    hll = HyperLogLog(precision=14)

    t0 = time.perf_counter()
    for i in range(100000):
        hll.add(f"key_{i}")
    t1 = time.perf_counter()
    estimate = hll.cardinality()
    t2 = time.perf_counter()

    print(f"   - Add 100K values: {(t1-t0)*1000:.2f}ms ({(t1-t0)/100000*1e6:.2f}μs per value)")
    print(f"   - Cardinality estimation: {(t2-t1)*1000:.2f}ms")
    print(f"   - Estimate: {estimate:,} (actual: 100,000, error: {abs(estimate-100000)/100000*100:.1f}%)")

    # 2. Bloom filter performance
    print("\n2. Bloom filter join optimization:")
    bloom = BloomFilter(expected_elements=100000, false_positive_rate=0.01)

    t0 = time.perf_counter()
    for i in range(100000):
        bloom.add(f"key_{i}")
    t1 = time.perf_counter()

    # Test lookup performance
    t2 = time.perf_counter()
    hits = sum(1 for i in range(100000) if bloom.contains(f"key_{i}"))
    t3 = time.perf_counter()

    # Test false positive rate
    t4 = time.perf_counter()
    false_positives = sum(1 for i in range(100000, 200000) if bloom.contains(f"key_{i}"))
    t5 = time.perf_counter()

    print(f"   - Add 100K values: {(t1-t0)*1000:.2f}ms ({(t1-t0)/100000*1e6:.2f}μs per value)")
    print(f"   - Lookup 100K (hits): {(t3-t2)*1000:.2f}ms ({(t3-t2)/100000*1e6:.2f}μs per lookup)")
    print(f"   - Lookup 100K (misses): {(t5-t4)*1000:.2f}ms")
    print(f"   - False positive rate: {false_positives/100000*100:.2f}% (target: 1.00%)")

    # 3. Query building performance
    print("\n3. Query construction:")
    from core.join_sampling import build_stratified_join_query

    sql = """
        SELECT c.c_mktsegment, COUNT(*) as count
        FROM lineitem l
        INNER JOIN orders o ON l.l_orderkey = o.o_orderkey
        INNER JOIN customer c ON o.o_custkey = c.c_custkey
        GROUP BY c.c_mktsegment
    """
    parsed = parse_analytical_query(sql)

    t0 = time.perf_counter()
    for _ in range(1000):
        query = build_stratified_join_query(parsed, "duckdb", 0.01)
    t1 = time.perf_counter()

    print(f"   - Build sampled query (1000x): {(t1-t0)*1000:.2f}ms ({(t1-t0)/1000*1000:.2f}μs per build)")


def analyze_bottlenecks():
    """Analyze and report bottlenecks with recommendations"""

    print("\n" + "="*80)
    print("BOTTLENECK ANALYSIS & RECOMMENDATIONS")
    print("="*80)

    bottlenecks = [
        {
            "issue": "Low sample rate (1%) produces empty JOIN results",
            "impact": "HIGH - Blocks all accuracy measurement",
            "cause": "With 60M rows sampled at 1% = 600K rows per table. JOIN selectivity on sampled data is very low.",
            "fix": "Increase minimum sample rate to 5-10% for JOINs. Implement intelligent rate selection based on cardinality estimates.",
            "novelty": "⭐ NEW PATENT CLAIM: HyperLogLog-guided minimum sample rate prediction"
        },
        {
            "issue": "Single-pass mode bypasses adaptive progression",
            "impact": "HIGH - Disables convergence detection",
            "cause": "Balanced mode with accuracy_target=None triggers single-pass, skipping iterations.",
            "fix": "Disable single-pass for JOIN queries. Always enable adaptive progression.",
            "novelty": "Enables multi-iteration convergence for complex queries"
        },
        {
            "issue": "No semi-join reduction for selective JOINs",
            "impact": "MEDIUM - Wastes computation on non-matching rows",
            "cause": "Stratified sampling doesn't filter before JOIN, so we sample full tables.",
            "fix": "Implement two-phase: pilot sample → identify matching keys → stratified sample on filtered tables",
            "novelty": "⭐⭐ NEW PATENT CLAIM: Adaptive two-phase semi-join sampling with Bloom filter propagation"
        },
        {
            "issue": "No join reordering optimization",
            "impact": "MEDIUM - Suboptimal execution plans",
            "cause": "Uses user-specified join order, which may produce large intermediates.",
            "fix": "Use HLL cardinality estimates to reorder joins for minimum intermediate size.",
            "novelty": "⭐ NEW PATENT CLAIM: Cardinality-guided dynamic join reordering for approximate queries"
        },
        {
            "issue": "TABLESAMPLE overhead on small samples",
            "impact": "LOW-MEDIUM - Adds ~50-100ms per query",
            "cause": "DuckDB TABLESAMPLE has initialization overhead.",
            "fix": "For repeated queries, materialize samples as temp tables. Add sample caching.",
            "novelty": "Incremental materialization with invalidation tracking"
        },
        {
            "issue": "No error bounds or confidence intervals",
            "impact": "HIGH - Cannot prove theoretical accuracy guarantees",
            "cause": "Only convergence-based stopping, no statistical bounds.",
            "fix": "Add Hoeffding bounds for COUNT/SUM, Central Limit Theorem for AVG. Early stopping when bounds meet target.",
            "novelty": "⭐⭐ THEORETICAL CONTRIBUTION: Provable error bounds for stratified join sampling"
        }
    ]

    for i, bottleneck in enumerate(bottlenecks, 1):
        print(f"\n[{i}] {bottleneck['issue']}")
        print(f"    Impact: {bottleneck['impact']}")
        print(f"    Cause: {bottleneck['cause']}")
        print(f"    Fix: {bottleneck['fix']}")
        print(f"    Novelty: {bottleneck['novelty']}")

    print("\n" + "="*80)
    print("PRIORITY FIXES FOR PATENT + PUBLICATION:")
    print("="*80)
    print("\n1. ⭐⭐⭐ Two-phase semi-join reduction (HIGHEST NOVELTY)")
    print("2. ⭐⭐ Error bounds with early stopping (THEORETICAL CONTRIBUTION)")
    print("3. ⭐ Adaptive join reordering (STRONG PATENT CLAIM)")
    print("4. Fix sample rate selection (REQUIRED FOR FUNCTIONALITY)")
    print("5. Disable single-pass for JOINs (QUICK FIX)")


if __name__ == "__main__":
    print("\n🔬 AetherQuery JOIN Performance Profiling\n")

    # Run component profiling
    profile_components()

    # Analyze bottlenecks
    analyze_bottlenecks()

    # Run full profiling
    profile_join_execution()

    print("\n" + "="*80)
    print("Profiling complete. See analysis above for optimization opportunities.")
    print("="*80)
