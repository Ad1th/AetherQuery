import { useState, useEffect } from "react";

interface HistoryItem {
  query: string;
  source: string;
  mode: string;
  time: number;
  result_rows?: number;
  timestamp?: string;
  cached?: boolean;
}

interface QueryCacheSidebarProps {
  onQueryClick?: (query: string, source: string) => void;
  currentQuery?: string;
}

export default function QueryCacheSidebar({
  onQueryClick,
  currentQuery = "",
}: QueryCacheSidebarProps) {
  // currentQuery reserved for future similar query detection feature
  void currentQuery;

  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [_loading, setLoading] = useState(false);
  const [searchText, setSearchText] = useState("");
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [isOpen, setIsOpen] = useState(true);

  const backend = "http://127.0.0.1:8093";

  useEffect(() => {
    fetchHistory();
    const interval = setInterval(fetchHistory, 5000);
    return () => clearInterval(interval);
  }, []);

  const fetchHistory = async () => {
    try {
      setLoading(true);
      const res = await fetch(`${backend}/history`);
      if (!res.ok) return;
      const data = await res.json();
      setHistory(data);
    } catch (err) {
      console.error("Failed to fetch history:", err);
    } finally {
      setLoading(false);
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
      if (!res.ok) {
        console.error("Failed to clear backend cache");
        return;
      }
      setHistory([]);
      setSelectedIndex(null);
    } catch (err) {
      console.error("Failed to clear backend cache:", err);
    }
  };

  const handleQueryClick = (item: HistoryItem, index: number) => {
    setSelectedIndex(index);
    if (onQueryClick) {
      onQueryClick(item.query, item.source);
    }
  };

  if (!isOpen) {
    return (
      <button
        onClick={() => setIsOpen(true)}
        style={{
          position: "fixed",
          right: "12px",
          top: "60px",
          zIndex: 51,
          background: "rgba(24,95,165,0.15)",
          color: "#5aaaf5",
          border: "0.5px solid rgba(24,95,165,0.3)",
          borderRadius: "6px",
          padding: "6px 10px",
          fontSize: "11px",
          cursor: "pointer",
        }}
      >
        Show Cache
      </button>
    );
  }

  return (
    <div
      style={{
        position: "fixed",
        right: 0,
        top: "48px",
        width: "320px",
        height: "calc(100vh - 48px)",
        background: "#141414",
        borderLeft: "0.5px solid rgba(255,255,255,0.08)",
        overflow: "hidden",
        zIndex: 50,
        display: "flex",
        flexDirection: "column",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "12px 14px",
          borderBottom: "0.5px solid rgba(255,255,255,0.06)",
          flexShrink: 0,
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "8px",
          }}
        >
          <h3
            style={{
              margin: 0,
              fontSize: "12px",
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: "0.5px",
              color: "#aaa",
            }}
          >
            Query Cache ({history.length})
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
              onClick={() => setIsOpen(false)}
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

        {/* Cache statistics */}
        {history.length > 0 && (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: "6px",
              marginBottom: "8px",
            }}
          >
            <div
              style={{
                background: "rgba(90,170,245,0.1)",
                padding: "6px 8px",
                borderRadius: "4px",
                fontSize: "10px",
                color: "#5aaaf5",
                border: "0.5px solid rgba(90,170,245,0.2)",
              }}
            >
              <div style={{ fontWeight: 600 }}>
                {Math.round(
                  (history.filter((q) => q.cached).length / history.length) *
                    100,
                )}
                %
              </div>
              <div style={{ color: "#888", fontSize: "9px" }}>hit rate</div>
            </div>
            <div
              style={{
                background: "rgba(76,175,80,0.1)",
                padding: "6px 8px",
                borderRadius: "4px",
                fontSize: "10px",
                color: "#76c576",
                border: "0.5px solid rgba(76,175,80,0.2)",
              }}
            >
              <div style={{ fontWeight: 600 }}>
                {history.filter((q) => q.cached).length}/{history.length}
              </div>
              <div style={{ color: "#888", fontSize: "9px" }}>cached</div>
            </div>
          </div>
        )}

        {/* Search box */}
        <input
          type="text"
          placeholder="Search queries..."
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          style={{
            width: "100%",
            padding: "6px 8px",
            background: "#1a1a1a",
            border: "0.5px solid rgba(255,255,255,0.08)",
            borderRadius: "4px",
            color: "#ccc",
            fontSize: "11px",
            outline: "none",
          }}
        />
      </div>

      {/* History List */}
      <div style={{ flex: 1, overflowY: "auto", padding: "4px 0" }}>
        {history.length === 0 ? (
          <div
            style={{
              padding: "16px 14px",
              color: "#666",
              fontSize: "11px",
              textAlign: "center",
            }}
          >
            {searchText ? "No queries match" : "No queries yet"}
          </div>
        ) : (
          history
            .filter(
              (item) =>
                item.query.toLowerCase().includes(searchText.toLowerCase()) ||
                item.source.toLowerCase().includes(searchText.toLowerCase()),
            )
            .map((item, i) => (
              <div
                key={i}
                onClick={() => handleQueryClick(item, history.indexOf(item))}
                style={{
                  padding: "8px 12px",
                  borderBottom: "0.5px solid rgba(255,255,255,0.04)",
                  cursor: "pointer",
                  transition: "all 0.15s",
                  background:
                    selectedIndex === history.indexOf(item)
                      ? "rgba(90,170,245,0.12)"
                      : "transparent",
                  borderLeft:
                    selectedIndex === history.indexOf(item)
                      ? "2px solid #5aaaf5"
                      : "2px solid transparent",
                  paddingLeft:
                    selectedIndex === history.indexOf(item) ? "10px" : "12px",
                }}
                onMouseEnter={(e) => {
                  if (selectedIndex !== history.indexOf(item)) {
                    e.currentTarget.style.background = "rgba(255,255,255,0.03)";
                  }
                }}
                onMouseLeave={(e) => {
                  if (selectedIndex !== history.indexOf(item)) {
                    e.currentTarget.style.background = "transparent";
                  }
                }}
              >
                <div
                  style={{ display: "flex", alignItems: "start", gap: "6px" }}
                >
                  {/* Cache indicator */}
                  {item.cached && (
                    <span
                      title="Retrieved from cache - instant result!"
                      style={{
                        fontSize: "11px",
                        color: "#5aaaf5",
                        fontWeight: 700,
                        whiteSpace: "nowrap",
                        marginTop: "0px",
                      }}
                    >
                      ⚡
                    </span>
                  )}

                  {/* Query text */}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <code
                      style={{
                        fontSize: "10px",
                        color: "#bbb",
                        display: "block",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        marginBottom: "3px",
                      }}
                      title={item.query}
                    >
                      {item.query}
                    </code>

                    {/* Metadata */}
                    <div
                      style={{
                        fontSize: "9px",
                        color: "#666",
                        display: "flex",
                        gap: "6px",
                        flexWrap: "wrap",
                      }}
                    >
                      <span
                        style={{
                          background: "rgba(90,170,245,0.15)",
                          padding: "1px 5px",
                          borderRadius: "2px",
                        }}
                      >
                        {item.source}
                      </span>
                      <span title="Execution time">
                        {item.cached ? "instant" : `${item.time.toFixed(3)}s`}
                      </span>
                      {item.result_rows !== undefined && (
                        <span title="Number of rows">
                          {item.result_rows} rows
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            ))
        )}
      </div>
    </div>
  );
}
