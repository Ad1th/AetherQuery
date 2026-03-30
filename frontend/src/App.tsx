import { useState } from "react";
import PlanGraph from "./components/PlanGraph";
import QueryCacheSidebar from "./components/QueryCacheSidebar";
import Spinner from "./components/Spinner";

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

function App() {
  const [tableName, setTableName] = useState<string | null>(null);
  const [queryExact, setQueryExact] = useState("");
  const [queryApprox, setQueryApprox] = useState("");
  const [planExact, setPlanExact] = useState<any | null>(null);
  const [planApprox, setPlanApprox] = useState<any | null>(null);
  const [resultExact, setResultExact] = useState<any | null>(null);
  const [resultApprox, setResultApprox] = useState<any | null>(null);
  const [errorExact, setErrorExact] = useState<string | null>(null);
  const [errorApprox, setErrorApprox] = useState<string | null>(null);
  const [sourceExact, setSourceExact] = useState<
    "duckdb" | "postgres" | "mysql"
  >("duckdb");
  const [sourceApprox, setSourceApprox] = useState<
    "duckdb" | "postgres" | "mysql"
  >("duckdb");
  const [suggestedQueries, setSuggestedQueries] = useState<string[]>([]);
  const [loadingExact, setLoadingExact] = useState(false);
  const [loadingApprox, setLoadingApprox] = useState(false);
  const [progressExact, setProgressExact] = useState<ExecutionProgress | null>(null);
  const [progressApprox, setProgressApprox] = useState<ExecutionProgress | null>(null);
  const [accuracyEnabledApprox, setAccuracyEnabledApprox] = useState(false);
  const [accuracyTargetApprox, setAccuracyTargetApprox] = useState(92);

  const backend = "http://127.0.0.1:8093";
  const csvMode = tableName !== null;

  const makeRequestId = () =>
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;

  const startProgressPolling = (
    requestId: string,
    setProgress: (value: ExecutionProgress | null) => void,
  ) => {
    let active = true;

    const poll = async () => {
      if (!active) return;
      try {
        const res = await fetch(`${backend}/api/sql/execute/progress/${requestId}`);
        if (res.ok) {
          const data = (await res.json()) as ExecutionProgress;
          setProgress(data);
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

  // -----------------------
  // Upload CSV
  // -----------------------
  const handleUpload = async (e: any) => {
    const file = e.target.files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${backend}/api/upload`, {
      method: "POST",
      body: form,
    });
    const data = await res.json();
    if (!data.table_name) {
      alert("Upload failed");
      return;
    }
    const tbl = data.table_name;
    setTableName(tbl);
    setSourceExact("duckdb");
    setSourceApprox("duckdb");
    setSuggestedQueries([
      `SELECT * FROM ${tbl} LIMIT 5;`,
      `SELECT COUNT(*) FROM ${tbl};`,
      `SELECT AVG(salary) FROM ${tbl};`,
    ]);
  };

  // -----------------------
  // Analyze
  // -----------------------
  const handleAnalyze = async (panel: "exact" | "approx") => {
    const query = panel === "exact" ? queryExact : queryApprox;
    const source = panel === "exact" ? sourceExact : sourceApprox;
    const setError = panel === "exact" ? setErrorExact : setErrorApprox;
    const setPlan = panel === "exact" ? setPlanExact : setPlanApprox;
    const setLoading = panel === "exact" ? setLoadingExact : setLoadingApprox;
    if (!query.trim()) return;
    setError(null);
    setLoading(true);
    try {
      const res = await fetch(`${backend}/api/sql/parse-plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, source: csvMode ? "duckdb" : source }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data?.detail || "Failed to analyze query");
        return;
      }
      setPlan(data.plan_tree ?? null);
    } finally {
      setLoading(false);
    }
  };

  // -----------------------
  // Execute
  // -----------------------
  const handleExecute = async (panel: "exact" | "approx") => {
    const query = panel === "exact" ? queryExact : queryApprox;
    const source = panel === "exact" ? sourceExact : sourceApprox;
    const setError = panel === "exact" ? setErrorExact : setErrorApprox;
    const setResult = panel === "exact" ? setResultExact : setResultApprox;
    const setLoading = panel === "exact" ? setLoadingExact : setLoadingApprox;
    const setProgress = panel === "exact" ? setProgressExact : setProgressApprox;
    if (!query.trim()) return;
    setError(null);
    setProgress(null);
    setLoading(true);
    const requestId = makeRequestId();
    const stopPolling = startProgressPolling(requestId, setProgress);
    try {
      const res = await fetch(`${backend}/api/sql/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query,
          mode: panel,
          source: csvMode ? "duckdb" : source,
          request_id: requestId,
          accuracy_target:
            panel === "approx" && accuracyEnabledApprox
              ? accuracyTargetApprox
              : undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setResult(null);
        setError(data?.detail || "Query execution failed");
        return;
      }
      setResult(data);
    } finally {
      stopPolling();
      setLoading(false);
    }
  };

  // -----------------------
  // Result Table renderer
  // -----------------------
  const renderAccuracyControl = () => (
    <div
      style={{
        background: accuracyEnabledApprox
          ? "linear-gradient(135deg, rgba(245,184,74,0.16), rgba(186,117,23,0.08))"
          : "linear-gradient(135deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02))",
        border: accuracyEnabledApprox
          ? "0.5px solid rgba(245,184,74,0.28)"
          : "0.5px solid rgba(255,255,255,0.08)",
        borderRadius: "12px",
        padding: "14px",
        display: "flex",
        flexDirection: "column",
        gap: "10px",
        opacity: accuracyEnabledApprox ? 1 : 0.62,
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
            checked={accuracyEnabledApprox}
            onChange={(e) => setAccuracyEnabledApprox(e.target.checked)}
            style={{ accentColor: "#f5b84a" }}
          />
          <div>
            <div style={{ fontSize: "12px", color: "#f0d2a3", fontWeight: 700 }}>
              Accuracy-Speed Tradeoff
            </div>
            <div style={{ fontSize: "11px", color: "#b5976b", marginTop: "2px" }}>
              Optional. Enable only if you want a specific target.
            </div>
          </div>
        </div>
        <div
          style={{
            minWidth: "72px",
            textAlign: "center",
            padding: "8px 10px",
            borderRadius: "999px",
            background: "rgba(15,15,15,0.55)",
            color: "#fff3da",
            fontSize: "18px",
            fontWeight: 700,
          }}
        >
          {accuracyTargetApprox}%
        </div>
      </div>
      <input
        type="range"
        min={70}
        max={99}
        step={1}
        value={accuracyTargetApprox}
        onChange={(e) => setAccuracyTargetApprox(Number(e.target.value))}
        disabled={!accuracyEnabledApprox}
        style={{ width: "100%", accentColor: "#f5b84a" }}
      />
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, 1fr)",
          gap: "8px",
        }}
      >
        {[
          { label: "Fast", value: 80, note: "lower latency" },
          { label: "Balanced", value: 92, note: "daily use" },
          { label: "Accurate", value: 98, note: "deeper scan" },
        ].map((preset) => (
          <button
            key={preset.label}
            type="button"
            onClick={() => setAccuracyTargetApprox(preset.value)}
            disabled={!accuracyEnabledApprox}
            style={{
              padding: "10px 12px",
              borderRadius: "10px",
              border:
                accuracyTargetApprox === preset.value
                  ? "0.5px solid rgba(245,184,74,0.5)"
                  : "0.5px solid rgba(255,255,255,0.08)",
              background:
                accuracyTargetApprox === preset.value
                  ? "rgba(245,184,74,0.18)"
                  : "rgba(255,255,255,0.03)",
              color: accuracyTargetApprox === preset.value ? "#fff0cf" : "#bcb2a1",
            }}
          >
            <div style={{ fontSize: "12px", fontWeight: 700 }}>{preset.label}</div>
            <div style={{ fontSize: "10px", marginTop: "2px" }}>
              {preset.value}% • {preset.note}
            </div>
          </button>
        ))}
      </div>
    </div>
  );

  const renderResult = (
    result: any,
    error: string | null,
    source: string,
    loading: boolean = false,
    progress: ExecutionProgress | null = null,
  ) => (
    <div style={{ marginTop: "12px" }}>
      {error && (
        <div
          style={{
            color: "#ff6b6b",
            fontSize: "12px",
            padding: "10px 12px",
            background: "rgba(255,107,107,0.1)",
            borderRadius: "8px",
            border: "0.5px solid rgba(255,107,107,0.3)",
            display: "flex",
            alignItems: "flex-start",
            gap: "8px",
          }}
        >
          <span style={{ fontSize: "14px", marginTop: "1px", flexShrink: 0 }}>
            ⚠️
          </span>
          <div>
            <strong style={{ display: "block", marginBottom: "3px" }}>
              Error
            </strong>
            <span>{error}</span>
          </div>
        </div>
      )}
      {loading && (
        <div
          style={{
            padding: "16px 12px",
            color: "#888",
            fontSize: "13px",
            display: "flex",
            flexDirection: "column",
            alignItems: "flex-start",
            gap: "8px",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "10px",
            }}
          >
            <Spinner size={14} />
            <span>{progress?.message ?? "Executing query…"}</span>
          </div>
          {progress && (
            <div
              style={{
                width: "100%",
                padding: "10px 12px",
                background: "rgba(255,255,255,0.03)",
                border: "0.5px solid rgba(255,255,255,0.08)",
                borderRadius: "8px",
                fontSize: "12px",
                color: "#aab4bf",
              }}
            >
              <div>Phase: {progress.phase ?? "running"}</div>
              {typeof progress.current_sample_fraction === "number" && (
                <div>
                  Sample: {(progress.current_sample_fraction * 100).toFixed(0)}%
                </div>
              )}
              {typeof progress.accuracy_target === "number" && (
                <div>Target accuracy: {progress.accuracy_target.toFixed(0)}%</div>
              )}
              {progress.iterations && progress.iterations.length > 0 && (
                <div>
                  Latest iteration: sampled{" "}
                  {progress.iterations[progress.iterations.length - 1].rows_sampled} rows
                </div>
              )}
            </div>
          )}
        </div>
      )}
      {result && !loading && (
        <div style={{ marginTop: "8px" }}>
          {result.cached && (
            <div
              style={{
                background: "rgba(90, 170, 245, 0.12)",
                border: "0.5px solid rgba(90, 170, 245, 0.3)",
                padding: "8px 12px",
                borderRadius: "6px",
                marginBottom: "8px",
                fontSize: "12px",
                color: "#5aaaf5",
                fontWeight: 600,
                display: "flex",
                alignItems: "center",
                gap: "8px",
              }}
            >
              <span style={{ fontSize: "14px" }}>⚡</span>
              <span>Result loaded from cache</span>
            </div>
          )}
          <div
            style={{
              fontSize: "12px",
              color: "#888",
              marginBottom: "8px",
              display: "flex",
              gap: "14px",
              flexWrap: "wrap",
            }}
          >
            <span>
              Mode:{" "}
              <span style={{ color: "#ccc" }}>
                {result.approx ? "approx" : "exact"}
              </span>
            </span>
            {result.accuracy_target && (
              <span>
                Accuracy target:{" "}
                <span style={{ color: "#f0d2a3" }}>{result.accuracy_target}%</span>
              </span>
            )}
            <span>
              Source:{" "}
              <span style={{ color: "#ccc" }}>{result.source ?? source}</span>
            </span>
            <span>
              {result.cached ? "Cache load time: " : "Time: "}
              <span style={{ color: result.cached ? "#5aaaf5" : "#ccc" }}>
                {typeof result.cache_load_time === "number"
                  ? `${result.cache_load_time.toFixed(6)}s`
                  : typeof result.time === "number"
                    ? `${result.time.toFixed(6)}s`
                    : "n/a"}
              </span>
            </span>
            {result.cached && (
              <span>
                Old time:{" "}
                <span style={{ color: "#ccc" }}>
                  {typeof result.old_time === "number"
                    ? `${result.old_time.toFixed(6)}s`
                    : "n/a"}
                </span>
              </span>
            )}
            {!result.cached && (
              <span style={{ display: "none" }}>
                {typeof result.time === "number"
                  ? `${result.time.toFixed(6)}s`
                  : "n/a"}
              </span>
            )}
            {result.sample_rate && (
              <span>
                Sample rate:{" "}
                <span style={{ color: "#ccc" }}>{result.sample_rate}</span>
              </span>
            )}
          </div>

          {result.rewritten_query && (
            <div style={{ marginBottom: "10px" }}>
              <div
                style={{ fontSize: "12px", color: "#888", marginBottom: "4px" }}
              >
                Rewritten query:
              </div>
              <pre
                style={{
                  background: "#111",
                  padding: "8px 10px",
                  borderRadius: "6px",
                  fontSize: "11px",
                  overflowX: "auto",
                  margin: 0,
                }}
              >
                {result.rewritten_query}
              </pre>
            </div>
          )}

          {Array.isArray(result.rows) && result.rows.length > 0 && (
            <div style={{ overflowX: "auto" }}>
              <table
                style={{
                  width: "100%",
                  borderCollapse: "collapse",
                  fontSize: "12px",
                }}
              >
                <thead>
                  <tr>
                    {(result.columns ?? []).map((c: string) => (
                      <th
                        key={c}
                        style={{
                          border: "0.5px solid #333",
                          padding: "6px 10px",
                          background: "#1a1a1a",
                          color: "#aaa",
                          textAlign: "left",
                          fontWeight: 500,
                        }}
                      >
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.rows.map((row: any[], idx: number) => (
                    <tr
                      key={idx}
                      style={{ background: idx % 2 === 0 ? "#141414" : "#111" }}
                    >
                      {row.map((v, i) => (
                        <td
                          key={i}
                          style={{
                            border: "0.5px solid #2a2a2a",
                            padding: "5px 10px",
                            color: "#e0ddd8",
                          }}
                        >
                          {String(v)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {!Array.isArray(result.rows) && result.result !== undefined && (
            <pre
              style={{
                background: "#111",
                padding: "10px",
                borderRadius: "6px",
                fontSize: "11px",
                overflowX: "auto",
              }}
            >
              {JSON.stringify(result.result, null, 2)}
            </pre>
          )}

          {Array.isArray(result.rows) && result.rows.length === 0 && (
            <p style={{ color: "#666", fontSize: "13px" }}>No rows returned.</p>
          )}
        </div>
      )}
      {!result && !error && !loading && (
        <div
          style={{
            color: "#444",
            fontSize: "12px",
            fontStyle: "italic",
            padding: "8px 0",
          }}
        >
          Results will appear here…
        </div>
      )}
    </div>
  );

  // Handle query click from cache sidebar
  const handleCacheQueryClick = (query: string, source: string) => {
    setQueryExact(query);
    setSourceExact(source as "duckdb" | "postgres" | "mysql");
    // Auto-execute in 100ms
    setTimeout(() => {
      handleExecute("exact");
    }, 100);
  };

  return (
    <>
      <QueryCacheSidebar
        onQueryClick={handleCacheQueryClick}
        currentQuery={queryExact}
      />
      <div
        style={{
          padding: "28px 32px 40px",
          color: "white",
          background:
            "radial-gradient(circle at top left, rgba(90,170,245,0.12), transparent 28%), radial-gradient(circle at top right, rgba(245,184,74,0.10), transparent 24%), #0f0f0f",
          minHeight: "100vh",
          fontFamily: "'Syne', sans-serif",
        }}
      >
        <div
          style={{
            marginBottom: "24px",
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
            Runtime Sampling Workspace
          </div>
          <h1
            style={{
              fontSize: "2.2rem",
              fontWeight: 700,
              letterSpacing: "-0.8px",
              marginBottom: "6px",
            }}
          >
            AetherQuery Executor
          </h1>
          <p style={{ color: "#7f8791", fontSize: "14px", maxWidth: "760px" }}>
            Run exact and approximate analytics side by side, tune the target
            accuracy, and watch the sampler refine results while it executes.
          </p>
        </div>

        {/* Upload bar */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "14px",
            padding: "10px 16px",
            background: "#1a1a1a",
            border: "0.5px solid rgba(255,255,255,0.1)",
            borderRadius: "8px",
            marginBottom: "20px",
          }}
        >
          <label
            style={{
              fontSize: "13px",
              fontWeight: 600,
              color: "#888",
              whiteSpace: "nowrap",
            }}
          >
            Upload CSV
          </label>
          <input
            type="file"
            onChange={handleUpload}
            style={{
              fontSize: "13px",
              color: "#ccc",
              flex: 1,
              background: "transparent",
              border: "none",
              outline: "none",
              cursor: "pointer",
            }}
          />
          {tableName && (
            <span
              style={{
                fontSize: "12px",
                color: "#5aaaf5",
                whiteSpace: "nowrap",
              }}
            >
              ✓ {tableName}
            </span>
          )}
        </div>

        {csvMode && (
          <p
            style={{
              color: "#5aaaf5",
              fontSize: "12px",
              marginBottom: "16px",
              padding: "6px 12px",
              background: "rgba(24,95,165,0.1)",
              borderRadius: "6px",
              border: "0.5px solid rgba(90,170,245,0.2)",
            }}
          >
            CSV mode active — queries are locked to DuckDB
          </p>
        )}

        {/* Suggested queries */}
        {tableName && suggestedQueries.length > 0 && (
          <div style={{ marginBottom: "20px" }}>
            <div
              style={{
                fontSize: "12px",
                color: "#666",
                marginBottom: "8px",
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.5px",
              }}
            >
              Suggested Queries
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
              {suggestedQueries.map((q, i) => (
                <div
                  key={i}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "8px",
                    background: "#1a1a1a",
                    border: "0.5px solid #2a2a2a",
                    borderRadius: "6px",
                    padding: "6px 10px",
                  }}
                >
                  <code style={{ fontSize: "12px", color: "#bbb" }}>{q}</code>
                  <button
                    onClick={() => {
                      setQueryExact(q);
                      setQueryApprox(q);
                    }}
                    style={{
                      fontSize: "11px",
                      padding: "3px 8px",
                      background: "#2a2a2a",
                      color: "#aaa",
                      border: "0.5px solid #3a3a3a",
                      borderRadius: "4px",
                      cursor: "pointer",
                    }}
                  >
                    Use
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Two columns */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: "14px",
          }}
        >
          {/* EXACT PANEL */}
          <div
            style={{
              background: "#141414",
              border: "0.5px solid rgba(255,255,255,0.08)",
              borderRadius: "12px",
              overflow: "hidden",
              boxShadow: "0 20px 45px rgba(0,0,0,0.18)",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "10px",
                padding: "12px 16px",
                borderBottom: "0.5px solid rgba(255,255,255,0.06)",
              }}
            >
              <span
                style={{
                  fontSize: "10px",
                  fontWeight: 700,
                  letterSpacing: "0.8px",
                  textTransform: "uppercase",
                  padding: "3px 9px",
                  borderRadius: "4px",
                  background: "rgba(24,95,165,0.18)",
                  color: "#5aaaf5",
                }}
              >
                Exact
              </span>
              <span style={{ fontSize: "14px", fontWeight: 600 }}>
                Exact Mode
              </span>
              <span
                style={{ fontSize: "12px", color: "#444", marginLeft: "auto" }}
              >
                Precise results
              </span>
            </div>
            <div
              style={{
                padding: "16px",
                display: "flex",
                flexDirection: "column",
                gap: "12px",
              }}
            >
              <div
                style={{ display: "flex", alignItems: "center", gap: "10px" }}
              >
                <span
                  style={{ fontSize: "12px", color: "#666", minWidth: "52px" }}
                >
                  Source
                </span>
                <select
                  value={sourceExact}
                  onChange={(e) => setSourceExact(e.target.value as any)}
                  disabled={csvMode}
                  style={{
                    fontFamily: "inherit",
                    fontSize: "13px",
                    padding: "5px 10px",
                    borderRadius: "6px",
                    border: "0.5px solid rgba(255,255,255,0.12)",
                    background: "#1e1e1e",
                    color: "#ccc",
                    cursor: csvMode ? "not-allowed" : "pointer",
                    opacity: csvMode ? 0.5 : 1,
                  }}
                >
                  <option value="duckdb">duckdb</option>
                  <option value="postgres">postgres</option>
                  <option value="mysql">mysql</option>
                </select>
                {csvMode && (
                  <span style={{ fontSize: "11px", color: "#555" }}>
                    locked to duckdb
                  </span>
                )}
              </div>
              <textarea
                value={queryExact}
                onChange={(e) => setQueryExact(e.target.value)}
                placeholder="SELECT * FROM data LIMIT 10;"
                style={{
                  width: "100%",
                  minHeight: "130px",
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
                }}
              />
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr",
                  gap: "8px",
                }}
              >
                <button
                  onClick={() => handleAnalyze("exact")}
                  disabled={loadingExact}
                  style={{
                    padding: "9px 12px",
                    fontFamily: "inherit",
                    fontWeight: 600,
                    fontSize: "13px",
                    borderRadius: "8px",
                    border: "0.5px solid rgba(24,95,165,0.3)",
                    background: loadingExact
                      ? "rgba(24,95,165,0.1)"
                      : "rgba(24,95,165,0.15)",
                    color: "#5aaaf5",
                    cursor: loadingExact ? "not-allowed" : "pointer",
                    opacity: loadingExact ? 0.6 : 1,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: "6px",
                  }}
                >
                  {loadingExact ? (
                    <>
                      <Spinner size={12} />
                      Analyzing…
                    </>
                  ) : (
                    "Analyze Query"
                  )}
                </button>
                <button
                  onClick={() => handleExecute("exact")}
                  disabled={loadingExact}
                  style={{
                    padding: "9px 12px",
                    fontFamily: "inherit",
                    fontWeight: 600,
                    fontSize: "13px",
                    borderRadius: "8px",
                    border: "none",
                    background: loadingExact ? "#0d4a8f" : "#185FA5",
                    color: "#fff",
                    cursor: loadingExact ? "not-allowed" : "pointer",
                    opacity: loadingExact ? 0.6 : 1,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: "6px",
                  }}
                >
                  {loadingExact ? (
                    <>
                      <Spinner size={12} />
                      Running…
                    </>
                  ) : (
                    "Run Query"
                  )}
                </button>
              </div>
              {renderResult(
                resultExact,
                errorExact,
                csvMode ? "duckdb" : sourceExact,
                loadingExact,
                progressExact,
              )}
              {planExact && (
                <div style={{ marginTop: "8px" }}>
                  <div
                    style={{
                      fontSize: "12px",
                      color: "#666",
                      marginBottom: "6px",
                    }}
                  >
                    Plan tree
                  </div>
                  <pre
                    style={{
                      background: "#0d0d0d",
                      padding: "10px",
                      borderRadius: "6px",
                      fontSize: "11px",
                      overflowX: "auto",
                      maxHeight: "180px",
                    }}
                  >
                    {JSON.stringify(planExact, null, 2)}
                  </pre>
                  <div
                    style={{
                      height: "400px",
                      marginTop: "10px",
                      border: "0.5px solid #2a2a2a",
                      borderRadius: "8px",
                      overflow: "hidden",
                    }}
                  >
                    <PlanGraph plan={planExact} />
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* APPROX PANEL */}
          <div
            style={{
              background: "#141414",
              border: "0.5px solid rgba(255,255,255,0.08)",
              borderRadius: "12px",
              overflow: "hidden",
              boxShadow: "0 20px 45px rgba(0,0,0,0.18)",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "10px",
                padding: "12px 16px",
                borderBottom: "0.5px solid rgba(255,255,255,0.06)",
              }}
            >
              <span
                style={{
                  fontSize: "10px",
                  fontWeight: 700,
                  letterSpacing: "0.8px",
                  textTransform: "uppercase",
                  padding: "3px 9px",
                  borderRadius: "4px",
                  background: "rgba(186,117,23,0.18)",
                  color: "#f5b84a",
                }}
              >
                Approx
              </span>
              <span style={{ fontSize: "14px", fontWeight: 600 }}>
                Approx Mode
              </span>
              <span
                style={{ fontSize: "12px", color: "#444", marginLeft: "auto" }}
              >
                Estimated results
              </span>
            </div>
            <div
              style={{
                padding: "16px",
                display: "flex",
                flexDirection: "column",
                gap: "12px",
              }}
            >
              <div
                style={{ display: "flex", alignItems: "center", gap: "10px" }}
              >
                <span
                  style={{ fontSize: "12px", color: "#666", minWidth: "52px" }}
                >
                  Source
                </span>
                <select
                  value={sourceApprox}
                  onChange={(e) => setSourceApprox(e.target.value as any)}
                  disabled={csvMode}
                  style={{
                    fontFamily: "inherit",
                    fontSize: "13px",
                    padding: "5px 10px",
                    borderRadius: "6px",
                    border: "0.5px solid rgba(255,255,255,0.12)",
                    background: "#1e1e1e",
                    color: "#ccc",
                    cursor: csvMode ? "not-allowed" : "pointer",
                    opacity: csvMode ? 0.5 : 1,
                  }}
                >
                  <option value="duckdb">duckdb</option>
                  <option value="postgres">postgres</option>
                  <option value="mysql">mysql</option>
                </select>
                {csvMode && (
                  <span style={{ fontSize: "11px", color: "#555" }}>
                    locked to duckdb
                  </span>
                )}
              </div>
              <textarea
                value={queryApprox}
                onChange={(e) => setQueryApprox(e.target.value)}
                placeholder="SELECT approx_count_distinct(id) FROM data;"
                style={{
                  width: "100%",
                  minHeight: "130px",
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
                }}
              />
              {renderAccuracyControl()}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr",
                  gap: "8px",
                }}
              >
                <button
                  onClick={() => handleAnalyze("approx")}
                  disabled={loadingApprox}
                  style={{
                    padding: "9px 12px",
                    fontFamily: "inherit",
                    fontWeight: 600,
                    fontSize: "13px",
                    borderRadius: "8px",
                    border: "0.5px solid rgba(186,117,23,0.3)",
                    background: loadingApprox
                      ? "rgba(186,117,23,0.1)"
                      : "rgba(186,117,23,0.15)",
                    color: "#f5b84a",
                    cursor: loadingApprox ? "not-allowed" : "pointer",
                    opacity: loadingApprox ? 0.6 : 1,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: "6px",
                  }}
                >
                  {loadingApprox ? (
                    <>
                      <Spinner size={12} />
                      Analyzing…
                    </>
                  ) : (
                    "Analyze Query"
                  )}
                </button>
                <button
                  onClick={() => handleExecute("approx")}
                  disabled={loadingApprox}
                  style={{
                    padding: "9px 12px",
                    fontFamily: "inherit",
                    fontWeight: 600,
                    fontSize: "13px",
                    borderRadius: "8px",
                    border: "none",
                    background: loadingApprox ? "#7d5c1a" : "#BA7517",
                    color: "#fff",
                    cursor: loadingApprox ? "not-allowed" : "pointer",
                    opacity: loadingApprox ? 0.6 : 1,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    gap: "6px",
                  }}
                >
                  {loadingApprox ? (
                    <>
                      <Spinner size={12} />
                      Running…
                    </>
                  ) : (
                    "Run Query"
                  )}
                </button>
              </div>
              {renderResult(
                resultApprox,
                errorApprox,
                csvMode ? "duckdb" : sourceApprox,
                loadingApprox,
                progressApprox,
              )}
              {planApprox && (
                <div style={{ marginTop: "8px" }}>
                  <div
                    style={{
                      fontSize: "12px",
                      color: "#666",
                      marginBottom: "6px",
                    }}
                  >
                    Plan tree
                  </div>
                  <pre
                    style={{
                      background: "#0d0d0d",
                      padding: "10px",
                      borderRadius: "6px",
                      fontSize: "11px",
                      overflowX: "auto",
                      maxHeight: "180px",
                    }}
                  >
                    {JSON.stringify(planApprox, null, 2)}
                  </pre>
                  <div
                    style={{
                      height: "400px",
                      marginTop: "10px",
                      border: "0.5px solid #2a2a2a",
                      borderRadius: "8px",
                      overflow: "hidden",
                    }}
                  >
                    <PlanGraph plan={planApprox} />
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Comparison Section - TODO: Fix JSX structure */}
        {false && (
          <div>Placeholder</div>
        )}
      </div>
    </>
  );
}

export default App;
