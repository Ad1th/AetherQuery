import { useState } from "react";
import PlanGraph from "./components/PlanGraph";

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

  const backend = "http://127.0.0.1:8093";
  const csvMode = tableName !== null;

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
    if (!query.trim()) return;
    setError(null);
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
  };

  // -----------------------
  // Execute
  // -----------------------
  const handleExecute = async (panel: "exact" | "approx") => {
    const query = panel === "exact" ? queryExact : queryApprox;
    const source = panel === "exact" ? sourceExact : sourceApprox;
    const setError = panel === "exact" ? setErrorExact : setErrorApprox;
    const setResult = panel === "exact" ? setResultExact : setResultApprox;
    if (!query.trim()) return;
    setError(null);
    const res = await fetch(`${backend}/api/sql/execute`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query,
        mode: panel,
        source: csvMode ? "duckdb" : source,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      setResult(null);
      setError(data?.detail || "Query execution failed");
      return;
    }
    setResult(data);
  };

  // -----------------------
  // Result Table renderer
  // -----------------------
  const renderResult = (result: any, error: string | null, source: string) => (
    <div style={{ marginTop: "12px" }}>
      {error && (
        <div
          style={{
            color: "#f07070",
            fontSize: "13px",
            padding: "8px 10px",
            background: "rgba(240,112,112,0.08)",
            borderRadius: "6px",
            border: "0.5px solid rgba(240,112,112,0.25)",
          }}
        >
          <strong>Error:</strong> {error}
        </div>
      )}
      {result && (
        <div style={{ marginTop: "8px" }}>
          <div
            style={{
              fontSize: "12px",
              color: "#888",
              marginBottom: "8px",
              display: "flex",
              gap: "14px",
            }}
          >
            <span>
              Mode:{" "}
              <span style={{ color: "#ccc" }}>
                {result.approx ? "approx" : "exact"}
              </span>
            </span>
            <span>
              Source:{" "}
              <span style={{ color: "#ccc" }}>{result.source ?? source}</span>
            </span>
            <span>
              Time:{" "}
              <span style={{ color: "#ccc" }}>
                {typeof result.time === "number"
                  ? `${result.time.toFixed(6)}s`
                  : "n/a"}
              </span>
            </span>
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
      {!result && !error && (
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

  return (
    <div
      style={{
        padding: "28px 32px",
        color: "white",
        background: "#0f0f0f",
        minHeight: "100vh",
        fontFamily: "'Syne', sans-serif",
      }}
    >
      {/* Header */}
      <h1
        style={{
          fontSize: "1.8rem",
          fontWeight: 700,
          letterSpacing: "-0.5px",
          marginBottom: "4px",
        }}
      >
        Query Executor Workspace
      </h1>
      <p style={{ color: "#666", fontSize: "13px", marginBottom: "24px" }}>
        Run exact and approximate queries side by side
      </p>

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
            style={{ fontSize: "12px", color: "#5aaaf5", whiteSpace: "nowrap" }}
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
        style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "14px" }}
      >
        {/* EXACT PANEL */}
        <div
          style={{
            background: "#141414",
            border: "0.5px solid rgba(255,255,255,0.08)",
            borderRadius: "12px",
            overflow: "hidden",
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
            <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
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
                style={{
                  padding: "9px 12px",
                  fontFamily: "inherit",
                  fontWeight: 600,
                  fontSize: "13px",
                  borderRadius: "8px",
                  border: "0.5px solid rgba(24,95,165,0.3)",
                  background: "rgba(24,95,165,0.15)",
                  color: "#5aaaf5",
                  cursor: "pointer",
                }}
              >
                Analyze Query
              </button>
              <button
                onClick={() => handleExecute("exact")}
                style={{
                  padding: "9px 12px",
                  fontFamily: "inherit",
                  fontWeight: 600,
                  fontSize: "13px",
                  borderRadius: "8px",
                  border: "none",
                  background: "#185FA5",
                  color: "#fff",
                  cursor: "pointer",
                }}
              >
                Run Query
              </button>
            </div>
            {renderResult(
              resultExact,
              errorExact,
              csvMode ? "duckdb" : sourceExact,
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
            <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
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
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: "8px",
              }}
            >
              <button
                onClick={() => handleAnalyze("approx")}
                style={{
                  padding: "9px 12px",
                  fontFamily: "inherit",
                  fontWeight: 600,
                  fontSize: "13px",
                  borderRadius: "8px",
                  border: "0.5px solid rgba(186,117,23,0.3)",
                  background: "rgba(186,117,23,0.15)",
                  color: "#f5b84a",
                  cursor: "pointer",
                }}
              >
                Analyze Query
              </button>
              <button
                onClick={() => handleExecute("approx")}
                style={{
                  padding: "9px 12px",
                  fontFamily: "inherit",
                  fontWeight: 600,
                  fontSize: "13px",
                  borderRadius: "8px",
                  border: "none",
                  background: "#BA7517",
                  color: "#fff",
                  cursor: "pointer",
                }}
              >
                Run Query
              </button>
            </div>
            {renderResult(
              resultApprox,
              errorApprox,
              csvMode ? "duckdb" : sourceApprox,
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
    </div>
  );
}

export default App;
