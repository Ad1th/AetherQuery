from aqp_eval.metrics.compare import compare_results


exact = [
    ["A", 100],
    ["B", 200],
    ["C", 300],
]

approx = [
    ["C", 303],
    ["A", 98],
    ["B", 205],
]

error = compare_results(
    exact,
    approx,
    group_key_columns=1,
)

print("Mean relative error:", error)
print("Error percent:", error * 100)
