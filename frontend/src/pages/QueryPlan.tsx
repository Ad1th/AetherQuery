import { useState, useEffect } from "react";
import PlanGraph from "../components/PlanGraph";
import Spinner from "../components/Spinner";
import ComparisonBar from "../components/ComparisonBar";

interface HistoryItem {
  query: string;
  source: string;
  mode: string;
  time: number;
  result_rows?: number;
  timestamp?: string;
  cached?: boolean;
}

interface ExecuteResponse {
  result?: unknown;
  rows?: unknown;
  columns?: string[];
  time?: number;
  approx?: boolean;
  sample_rate?: number | string | null;
  source?: string;
  rewritten_query?: string | null;
  cached?: boolean;
  request_id?: string;
  accuracy_target?: number | null;
  old_time?: number;
  cache_load_time?: number;
}

interface ExecutionProgress {
  status?: string;
  phase?: string;
  message?: string;
  current_sample_fraction?: number;
  accuracy_target?: number;
  iterations?: Array<{
    sample_fraction: number;
    rows_sampled: number;
    elapsed_time: number;
    convergence_error?: number | null;
  }>;
}

interface ComparisonState {
  exactValue: number | null;
  approxValue: number | null;
  exactTime: number | null;
  approxTime: number | null;
  approxError: string | null;
}

interface OptimizeResponse {
  rewritten_query?: string;
}

export default function QueryPlanPage() {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<"duckdb" | "postgres" | "mysql">(
    "duckdb",
  );
  const [plan, setPlan] = useState<any | null>(null);
  const [planExplanation, setPlanExplanation] = useState<string | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [isExecuting, setIsExecuting] = useState(false);
  const [isOptimizing, setIsOptimizing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [optimizeNote, setOptimizeNote] = useState<string | null>(null);
  const [comparison, setComparison] = useState<ComparisonState | null>(null);
  const [executionProgress, setExecutionProgress] =
    useState<ExecutionProgress | null>(null);
  const [accuracyEnabled, setAccuracyEnabled] = useState(false);
  const [accuracyTarget, setAccuracyTarget] = useState(92);
  const [isSidebarOpen, setIsSidebarOpen] = useState(true);
  const [lastAction, setLastAction] = useState<
    "analyze" | "execute" | "optimize" | null
  >(null);
  const [activeHistoryIndex, setActiveHistoryIndex] = useState<number | null>(
    null,
  );

  const backend = "http://127.0.0.1:8093";
  const loading = isAnalyzing || isExecuting || isOptimizing;

  const makeRequestId = () =>
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;

  const startProgressPolling = (requestId: string) => {
    let active = true;

    const poll = async () => {
      if (!active) return;
      try {
        const res = await fetch(`${backend}/api/sql/execute/progress/${requestId}`);
        if (res.ok) {
          const data = (await res.json()) as ExecutionProgress;
          setExecutionProgress(data);
          if (data.status === "completed" || data.status === "error") {
            active = false;
            return;
          }
        }
      } catch {
        return;
      }
      if (active) {
        setTimeout(poll, 350);
      }
    };

    void poll();
    return () => {
      active = false;
    };
  };

  // Fetch history once on mount
  useEffect(() => {
    fetchHistory();
    const interval = setInterval(fetchHistory, 5000); // Refresh every 5s
    return () => clearInterval(interval);
  }, []);

  const fetchHistory = async () => {
    try {
      const res = await fetch(`${backend}/history`);
      if (!res.ok) return;
      const data = await res.json();
      setHistory(data);
    } catch (err) {
      console.error("Failed to fetch history:", err);
    }
  };

  const clearBackendCache = async () => {
    const confirmed = confirm(
      "Clear query cache and history from backend storage?",
    );
    if (!confirmed) return;

    try {
      const res = await fetch(`${backend}/api/cache/clear`, {
        method: "POST",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(parseBackendError(data, "Failed to clear backend cache"));
        return;
      }

      setHistory([]);
      setActiveHistoryIndex(null);
    } catch (err) {
      setError(`Error: ${String(err)}`);
    }
  };

  const parseBackendError = (data: unknown, fallback: string) => {
    if (
      typeof data === "object" &&
      data !== null &&
      "detail" in data &&
      typeof (data as { detail?: unknown }).detail === "string"
    ) {
      return (data as { detail: string }).detail;
    }
    return fallback;
  };

  const extractScalarValue = (response: ExecuteResponse) => {
    if (typeof response.result === "number" && Number.isFinite(response.result)) {
      return response.result;
    }

    if (Array.isArray(response.rows) && response.rows.length > 0) {
      const firstRow = response.rows[0];
      if (Array.isArray(firstRow) && firstRow.length > 0) {
        const firstCell = firstRow[0];
        if (typeof firstCell === "number" && Number.isFinite(firstCell)) {
          return firstCell;
        }
      }
    }

    return null;
  };

  const formatNumber = (value: number | null) => {
    if (value === null) return "n/a";
    return new Intl.NumberFormat().format(value);
  };

  const fetchPlanForQuery = async (queryText: string) => {
    try {
      const res = await fetch(`${backend}/api/sql/parse-plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: queryText, source }),
      });

      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(parseBackendError(data, "Failed to parse plan"));
        return false;
      }

      if (typeof data === "object" && data !== null) {
        const typedData = data as { plan_tree?: unknown; explanation?: unknown };
        setPlan((typedData.plan_tree as any) ?? null);
        setPlanExplanation(
          typeof typedData.explanation === "string" ? typedData.explanation : null,
        );
      } else {
        setPlan(null);
        setPlanExplanation(null);
      }
      return true;
    } catch (err) {
      setError(`Error: ${String(err)}`);
      return false;
    }
  };

  const runQuery = async (queryText: string, mode: "exact" | "approx") => {
    const requestId = makeRequestId();
    const stopPolling = startProgressPolling(requestId);
    try {
      const res = await fetch(`${backend}/api/sql/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: queryText,
          mode,
          source,
          request_id: requestId,
          accuracy_target:
            mode === "approx" && accuracyEnabled ? accuracyTarget : undefined,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(parseBackendError(data, "Query failed"));
      }
      return data as ExecuteResponse;
    } finally {
      stopPolling();
    }
  };

  const optimizeQuery = async () => {
    if (!query.trim()) {
      setError("Please enter a query");
      return;
    }

    setLastAction("optimize");
    setIsOptimizing(true);
    setError(null);
    setOptimizeNote(null);

    try {
      const res = await fetch(`${backend}/api/optimize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query,
          mode: "exact",
          source,
        }),
      });
      const data = (await res.json().catch(() => ({}))) as OptimizeResponse;
      if (!res.ok) {
        setError(parseBackendError(data, "Failed to optimize query"));
        return;
      }

      if (data.rewritten_query && data.rewritten_query.trim()) {
        setQuery(data.rewritten_query);
        setOptimizeNote("Optimized query applied to editor.");
      } else {
        setOptimizeNote("No optimization rewrite was returned for this query.");
      }
    } catch (err) {
      setError(`Error: ${String(err)}`);
    } finally {
      setIsOptimizing(false);
    }
  };

  // Analyze query plan
  const analyzeQuery = async (q?: string) => {
    const queryText = q ?? query;
    if (!queryText.trim()) {
      setError("Please enter a query");
      return;
    }

    setLastAction("analyze");
    setIsAnalyzing(true);
    setError(null);
    setPlan(null);
    setPlanExplanation(null);

    try {
      await fetchPlanForQuery(queryText);
    } finally {
      setIsAnalyzing(false);
    }
  };

  // Execute query (exact + approx comparison)
  const executeQuery = async (q?: string) => {
    const queryText = q ?? query;
    if (!queryText.trim()) {
      setError("Please enter a query");
      return;
    }

    setLastAction("execute");
    setIsExecuting(true);
    setError(null);
    setComparison(null);
    setExecutionProgress(null);

    try {
      const exactResult = await runQuery(queryText, "exact");

      let approxResult: ExecuteResponse | null = null;
      let approxError: string | null = null;
      try {
        approxResult = await runQuery(queryText, "approx");
      } catch (err) {
        approxError = String(err);
      }

      setComparison({
        exactValue: extractScalarValue(exactResult),
        approxValue: approxResult ? extractScalarValue(approxResult) : null,
        exactTime:
          typeof exactResult.old_time === "number"
            ? exactResult.old_time
            : typeof exactResult.time === "number"
              ? exactResult.time
              : null,
        approxTime:
          approxResult
            ? typeof approxResult.old_time === "number"
              ? approxResult.old_time
              : typeof approxResult.time === "number"
                ? approxResult.time
                : null
            : null,
        approxError,
      });

      await fetchPlanForQuery(queryText);
      await fetchHistory();
    } catch (err) {
      setError(`Error: ${String(err)}`);
    } finally {
      setIsExecuting(false);
    }
  };

  // Handle sidebar click
  const handleHistoryClick = (item: HistoryItem, index: number) => {
    setQuery(item.query);
    setSource((item.source as "duckdb" | "postgres" | "mysql") || "duckdb");
    setActiveHistoryIndex(index);
    analyzeQuery(item.query);
  };

  const renderAccuracyControl = () => (
    <div
      style={{
        padding: "14px",
        background: accuracyEnabled
          ? "linear-gradient(135deg, rgba(245,184,74,0.14), rgba(255,255,255,0.02))"
          : "linear-gradient(135deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02))",
        border: accuracyEnabled
          ? "0.5px solid rgba(245,184,74,0.24)"
          : "0.5px solid rgba(255,255,255,0.08)",
        borderRadius: "12px",
        display: "flex",
        flexDirection: "column",
        gap: "10px",
        opacity: accuracyEnabled ? 1 : 0.62,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          gap: "12px",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
          <input
            type="checkbox"
            checked={accuracyEnabled}
            onChange={(e) => setAccuracyEnabled(e.target.checked)}
            style={{ accentColor: "#f5b84a" }}
          />
          <div>
            <div style={{ fontSize: "12px", fontWeight: 700, color: "#f0d2a3" }}>
              Accuracy Target
            </div>
            <div style={{ fontSize: "11px", color: "#b5976b", marginTop: "2px" }}>
              Optional. Enable it only when you want a fixed target.
            </div>
          </div>
        </div>
        <div
          style={{
            padding: "8px 10px",
            borderRadius: "999px",
            background: "rgba(15,15,15,0.45)",
            color: "#fff4dc",
            fontSize: "18px",
            fontWeight: 700,
            minWidth: "72px",
            textAlign: "center",
          }}
        >
          {accuracyTarget}%
        </div>
      </div>
      <input
        type="range"
        min={70}
        max={99}
        step={1}
        value={accuracyTarget}
        onChange={(e) => setAccuracyTarget(Number(e.target.value))}
        disabled={!accuracyEnabled}
        style={{ width: "100%", accentColor: "#f5b84a" }}
      />
    </div>
  );

  return (
    <div
      style={{
        display: "flex",
        gap: "14px",
        padding: "24px",
        background:
          "radial-gradient(circle at top left, rgba(90,170,245,0.12), transparent 28%), radial-gradient(circle at top right, rgba(245,184,74,0.10), transparent 24%), #0f0f0f",
        minHeight: "100vh",
        color: "#e0ddd8",
        fontFamily: "'Syne', sans-serif",
      }}
    >
      {/* SIDEBAR */}
      {isSidebarOpen && (
        <div
          style={{
            width: "280px",
            background: "#141414",
            border: "0.5px solid rgba(255,255,255,0.08)",
            borderRadius: "12px",
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
          }}
        >
        {/* Header */}
        <div
          style={{
            padding: "12px 16px",
            borderBottom: "0.5px solid rgba(255,255,255,0.06)",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <h3
            style={{
              margin: 0,
              fontSize: "13px",
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: "0.5px",
              color: "#aaa",
            }}
          >
            Recent Queries
          </h3>
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <button
              onClick={clearBackendCache}
              title="Clear cache"
              style={{
                background: "transparent",
                border: "0.5px solid rgba(255,255,255,0.16)",
                color: "#999",
                cursor: "pointer",
                fontSize: "10px",
                borderRadius: "4px",
                padding: "2px 6px",
              }}
            >
              Clear Cache
            </button>
            <button
              onClick={() => setIsSidebarOpen(false)}
              title="Close sidebar"
              style={{
                background: "transparent",
                border: "none",
                color: "#666",
                cursor: "pointer",
                fontSize: "12px",
                padding: 0,
              }}
            >
              ✕
            </button>
          </div>
        </div>

        {/* History List */}
        <div style={{ flex: 1, overflowY: "auto" }}>
          {history.length === 0 ? (
            <div style={{ padding: "16px", color: "#666", fontSize: "12px" }}>
              No queries yet. Write and run a query to see history.
            </div>
          ) : (
            history.map((item, i) => (
              <div
                key={i}
                onClick={() => handleHistoryClick(item, i)}
                style={{
                  padding: "10px 12px",
                  borderBottom: "0.5px solid rgba(255,255,255,0.04)",
                  cursor: "pointer",
                  background:
                    activeHistoryIndex === i
                      ? "rgba(90,170,245,0.1)"
                      : "transparent",
                  transition: "background 0.15s",
                }}
                onMouseEnter={(e) => {
                  if (activeHistoryIndex !== i) {
                    e.currentTarget.style.background = "rgba(255,255,255,0.03)";
                  }
                }}
                onMouseLeave={(e) => {
                  if (activeHistoryIndex !== i) {
                    e.currentTarget.style.background = "transparent";
                  }
                }}
              >
                <code
                  style={{
                    fontSize: "11px",
                    color: "#bbb",
                    display: "block",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    marginBottom: "4px",
                  }}
                >
                  {item.query}
                </code>
                <div style={{ fontSize: "10px", color: "#666" }}>
                  <span style={{ marginRight: "8px" }}>{item.source}</span>
                  <span style={{ marginRight: "8px" }}>
                    {item.time.toFixed(3)}s
                  </span>
                  {item.cached && (
                    <span style={{ color: "#5aaaf5" }}>cached</span>
                  )}
                </div>
              </div>
            ))
          )}
        </div>
        </div>
      )}

      {/* MAIN PANEL */}
      <div
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          gap: "14px",
        }}
      >
        {/* Header */}
        <div>
          {!isSidebarOpen && (
            <button
              onClick={() => setIsSidebarOpen(true)}
              style={{
                marginBottom: "8px",
                background: "rgba(24,95,165,0.15)",
                color: "#5aaaf5",
                border: "0.5px solid rgba(24,95,165,0.3)",
                borderRadius: "6px",
                padding: "6px 10px",
                fontSize: "11px",
                cursor: "pointer",
              }}
            >
              Show Sidebar
            </button>
          )}
          <div
            style={{
              padding: "20px 22px",
              borderRadius: "18px",
              background:
                "linear-gradient(135deg, rgba(255,255,255,0.05), rgba(255,255,255,0.015))",
              border: "0.5px solid rgba(255,255,255,0.08)",
              boxShadow: "0 24px 60px rgba(0,0,0,0.25)",
            }}
          >
            <div
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "8px",
                padding: "6px 10px",
                borderRadius: "999px",
                background: "rgba(90,170,245,0.12)",
                border: "0.5px solid rgba(90,170,245,0.24)",
                color: "#9dccfb",
                fontSize: "11px",
                fontWeight: 700,
                letterSpacing: "0.3px",
                textTransform: "uppercase",
                marginBottom: "14px",
              }}
            >
              Exact vs Approx Explorer
            </div>
            <h1
              style={{
                fontSize: "2.2rem",
                fontWeight: 700,
                letterSpacing: "-0.8px",
                margin: "0 0 6px 0",
              }}
            >
              AetherQuery Plan Analyzer
            </h1>
            <p style={{ color: "#7f8791", fontSize: "14px", margin: 0 }}>
              Inspect plan shape, compare exact and approximate execution, and
              tune the target accuracy from the same workspace.
            </p>
          </div>
        </div>

        {/* Source Selector */}
        <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
          <span style={{ fontSize: "12px", color: "#666" }}>Source:</span>
          <select
            value={source}
            disabled={loading}
            onChange={(e) =>
              setSource(e.target.value as "duckdb" | "postgres" | "mysql")
            }
            style={{
              fontFamily: "inherit",
              fontSize: "13px",
              padding: "6px 10px",
              borderRadius: "6px",
              border: "0.5px solid rgba(255,255,255,0.12)",
              background: "#1a1a1a",
              color: "#ccc",
              cursor: loading ? "not-allowed" : "pointer",
              opacity: loading ? 0.7 : 1,
            }}
          >
            <option value="duckdb">DuckDB</option>
            <option value="postgres">PostgreSQL</option>
            <option value="mysql">MySQL</option>
          </select>
        </div>

        {/* Query Input */}
        <div>
          <label
            style={{
              fontSize: "12px",
              color: "#666",
              display: "block",
              marginBottom: "6px",
            }}
          >
            SQL Query
          </label>
          <textarea
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            disabled={loading}
            placeholder="SELECT * FROM table LIMIT 10;"
            style={{
              width: "100%",
              minHeight: "140px",
              padding: "10px 12px",
              background: "#1a1a1a",
              border: "0.5px solid rgba(255,255,255,0.08)",
              borderRadius: "8px",
              color: "#e0ddd8",
              fontFamily: "'JetBrains Mono', monospace",
              fontSize: "12.5px",
              lineHeight: 1.65,
              resize: "vertical",
              outline: "none",
              opacity: loading ? 0.75 : 1,
            }}
          />
        </div>

        {renderAccuracyControl()}

        {/* Buttons */}
        <div style={{ display: "flex", gap: "8px" }}>
          <button
            onClick={() => analyzeQuery()}
            disabled={loading}
            style={{
              flex: 1,
              padding: "9px 12px",
              fontFamily: "inherit",
              fontWeight: 600,
              fontSize: "13px",
              borderRadius: "8px",
              border: "0.5px solid rgba(24,95,165,0.3)",
              background: "rgba(24,95,165,0.15)",
              color: "#5aaaf5",
              cursor: loading ? "not-allowed" : "pointer",
              opacity: loading ? 0.6 : 1,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "8px",
            }}
          >
            {isAnalyzing ? (
              <>
                <Spinner size={12} />
                Analyzing...
              </>
            ) : (
              "Analyze Plan"
            )}
          </button>
          <button
            onClick={optimizeQuery}
            disabled={loading}
            style={{
              flex: 1,
              padding: "9px 12px",
              fontFamily: "inherit",
              fontWeight: 600,
              fontSize: "13px",
              borderRadius: "8px",
              border: "0.5px solid rgba(186,117,23,0.3)",
              background: "rgba(186,117,23,0.15)",
              color: "#f5b84a",
              cursor: loading ? "not-allowed" : "pointer",
              opacity: loading ? 0.6 : 1,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "8px",
            }}
          >
            {isOptimizing ? (
              <>
                <Spinner size={12} />
                Optimizing...
              </>
            ) : (
              "Optimize Query"
            )}
          </button>
          <button
            onClick={() => executeQuery()}
            disabled={loading}
            style={{
              flex: 1,
              padding: "9px 12px",
              fontFamily: "inherit",
              fontWeight: 600,
              fontSize: "13px",
              borderRadius: "8px",
              border: "none",
              background: "#185FA5",
              color: "#fff",
              cursor: loading ? "not-allowed" : "pointer",
              opacity: loading ? 0.6 : 1,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "8px",
            }}
          >
            {isExecuting ? (
              <>
                <Spinner size={12} />
                Running...
              </>
            ) : (
              "Execute"
            )}
          </button>
        </div>

        {loading && (
        <div
          style={{
              fontSize: "12px",
              color: "#7f95ad",
              display: "flex",
              flexDirection: "column",
              alignItems: "flex-start",
              gap: "8px",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <Spinner size={12} />
              {isOptimizing
                ? "Optimizing query with rewrite engine..."
                : isExecuting
                ? executionProgress?.message ?? "Executing exact and approximate queries..."
                : "Analyzing execution plan..."}
            </div>
            {isExecuting && executionProgress && (
              <div
                style={{
                  padding: "10px 12px",
                  background: "rgba(255,255,255,0.03)",
                  border: "0.5px solid rgba(255,255,255,0.08)",
                  borderRadius: "8px",
                  fontSize: "12px",
                  color: "#aab4bf",
                }}
              >
                <div>Phase: {executionProgress.phase ?? "running"}</div>
                {typeof executionProgress.current_sample_fraction === "number" && (
                  <div>
                    Sample: {(executionProgress.current_sample_fraction * 100).toFixed(0)}%
                  </div>
                )}
                {typeof executionProgress.accuracy_target === "number" && (
                  <div>
                    Target accuracy: {executionProgress.accuracy_target.toFixed(0)}%
                  </div>
                )}
                {executionProgress.iterations &&
                  executionProgress.iterations.length > 0 && (
                    <div>
                      Latest iteration: sampled{" "}
                      {
                        executionProgress.iterations[
                          executionProgress.iterations.length - 1
                        ].rows_sampled
                      }{" "}
                      rows
                    </div>
                  )}
              </div>
            )}
          </div>
        )}

        {optimizeNote && (
          <div
            style={{
              fontSize: "12px",
              color: "#d9bf8f",
              background: "rgba(245,184,74,0.09)",
              border: "0.5px solid rgba(245,184,74,0.22)",
              borderRadius: "6px",
              padding: "8px 10px",
            }}
          >
            {optimizeNote}
          </div>
        )}

        {/* Error */}
        {error && (
          <div
            style={{
              color: "#f07070",
              fontSize: "12px",
              padding: "10px 12px",
              background: "rgba(240,112,112,0.08)",
              borderRadius: "6px",
              border: "0.5px solid rgba(240,112,112,0.25)",
            }}
          >
            <div style={{ fontWeight: 700, marginBottom: "4px" }}>
              Query failed
            </div>
            <div style={{ marginBottom: "8px" }}>{error}</div>
            <div style={{ display: "flex", gap: "8px" }}>
              <button
                onClick={() =>
                  lastAction === "analyze"
                    ? analyzeQuery()
                    : lastAction === "optimize"
                      ? optimizeQuery()
                      : executeQuery()
                }
                disabled={loading}
                style={{
                  padding: "6px 10px",
                  background: "rgba(240,112,112,0.16)",
                  color: "#f7b4b4",
                  border: "0.5px solid rgba(240,112,112,0.35)",
                  borderRadius: "6px",
                  fontSize: "11px",
                  cursor: loading ? "not-allowed" : "pointer",
                }}
              >
                Retry
              </button>
              <button
                onClick={() => setError(null)}
                style={{
                  padding: "6px 10px",
                  background: "transparent",
                  color: "#c39a9a",
                  border: "0.5px solid rgba(240,112,112,0.3)",
                  borderRadius: "6px",
                  fontSize: "11px",
                  cursor: "pointer",
                }}
              >
                Dismiss
              </button>
            </div>
          </div>
        )}

        {/* Exact vs Approx Comparison */}
        {comparison && (
          <div
            style={{
              padding: "10px 12px",
              background: "rgba(255,255,255,0.02)",
              border: "0.5px solid rgba(255,255,255,0.08)",
              borderRadius: "8px",
            }}
          >
            <div
              style={{
                fontSize: "12px",
                color: "#666",
                marginBottom: "8px",
                textTransform: "uppercase",
                letterSpacing: "0.4px",
              }}
            >
              Exact vs Approx
            </div>
            <ComparisonBar
              label="Result Comparison"
              exactValue={comparison.exactValue ?? "n/a"}
              approxValue={comparison.approxValue ?? "n/a"}
              exactTimeSeconds={comparison.exactTime}
              approxTimeSeconds={comparison.approxTime}
            />
            <div
              title={`Exact: ${formatNumber(comparison.exactValue)} | Approx: ${formatNumber(
                comparison.approxValue,
              )}`}
              style={{
                fontSize: "11px",
                color: "#9db2c7",
                lineHeight: 1.6,
              }}
            >
              Exact: {formatNumber(comparison.exactValue)} | Approx:{" "}
              {formatNumber(comparison.approxValue)}
            </div>
            {comparison.approxError && (
              <div
                style={{
                  marginTop: "6px",
                  fontSize: "11px",
                  color: "#d6b47e",
                }}
              >
                Approx run note: {comparison.approxError}
              </div>
            )}
          </div>
        )}

        {/* Plan Explanation */}
        {planExplanation && (
          <div
            style={{
              padding: "10px 12px",
              background: "rgba(90,170,245,0.08)",
              borderRadius: "6px",
              border: "0.5px solid rgba(90,170,245,0.2)",
              fontSize: "12px",
              color: "#bbb",
            }}
          >
            {planExplanation}
          </div>
        )}

        {/* Plan Tree JSON */}
        {plan && (
          <div>
            <label
              style={{
                fontSize: "12px",
                color: "#666",
                display: "block",
                marginBottom: "6px",
              }}
            >
              Plan Tree
            </label>
            <pre
              style={{
                background: "#0d0d0d",
                padding: "10px",
                borderRadius: "6px",
                fontSize: "11px",
                overflowX: "auto",
                maxHeight: "200px",
                margin: 0,
                border: "0.5px solid rgba(255,255,255,0.08)",
              }}
            >
              {JSON.stringify(plan, null, 2)}
            </pre>
          </div>
        )}

        {/* Plan Visualization */}
        {plan && (
          <div>
            <label
              style={{
                fontSize: "12px",
                color: "#666",
                display: "block",
                marginBottom: "6px",
              }}
            >
              Execution Graph
            </label>
            <div
              style={{
                height: "450px",
                border: "0.5px solid rgba(255,255,255,0.08)",
                borderRadius: "8px",
                overflow: "hidden",
                background: "#0d0d0d",
              }}
            >
              <PlanGraph plan={plan} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
