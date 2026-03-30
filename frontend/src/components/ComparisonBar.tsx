export default function ComparisonBar({
  label,
  exactValue,
  approxValue,
  unit = "",
  exactTimeSeconds,
  approxTimeSeconds,
}: {
  label: string;
  exactValue: number | string;
  approxValue: number | string;
  unit?: string;
  exactTimeSeconds?: number | null;
  approxTimeSeconds?: number | null;
}) {
  const formatValue = (val: string | number) => {
    if (typeof val === "string") return val;
    return new Intl.NumberFormat().format(val);
  };

  const exactNumeric = typeof exactValue === "number" ? exactValue : null;
  const approxNumeric = typeof approxValue === "number" ? approxValue : null;
  const maxValue =
    exactNumeric !== null && approxNumeric !== null
      ? Math.max(Math.abs(exactNumeric), Math.abs(approxNumeric), 1)
      : 1;
  const exactWidth =
    exactNumeric !== null ? `${(Math.abs(exactNumeric) / maxValue) * 100}%` : "0%";
  const approxWidth =
    approxNumeric !== null
      ? `${(Math.abs(approxNumeric) / maxValue) * 100}%`
      : "0%";

  const showError = exactNumeric !== null && approxNumeric !== null && exactNumeric !== 0;
  const errorPercent = showError
    ? (Math.abs(exactNumeric - approxNumeric) / Math.abs(exactNumeric)) * 100
    : null;

  const hasTimeComparison =
    typeof exactTimeSeconds === "number" &&
    typeof approxTimeSeconds === "number" &&
    exactTimeSeconds > 0;
  const fasterPercent = hasTimeComparison
    ? ((exactTimeSeconds - approxTimeSeconds) / exactTimeSeconds) * 100
    : null;

  return (
    <div
      style={{
        marginBottom: "10px",
        padding: "10px 12px",
        background: "rgba(255,255,255,0.02)",
        border: "0.5px solid rgba(255,255,255,0.08)",
        borderRadius: "6px",
      }}
    >
      <div
        style={{
          fontSize: "11px",
          color: "#666",
          marginBottom: "6px",
          fontWeight: 500,
        }}
      >
        {label}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
        <div title={`Exact: ${formatValue(exactValue)}${unit ? ` ${unit}` : ""}`}>
          <div
            style={{
              fontSize: "10px",
              color: "#8aa8c5",
              marginBottom: "2px",
              display: "flex",
              justifyContent: "space-between",
            }}
          >
            <span>Exact</span>
            <span>
              {formatValue(exactValue)}
              {unit ? ` ${unit}` : ""}
            </span>
          </div>
          <div
            style={{
              height: "8px",
              borderRadius: "999px",
              background: "rgba(255,255,255,0.08)",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                height: "100%",
                width: exactWidth,
                background: "#5aaaf5",
              }}
            />
          </div>
        </div>
        <div title={`Approx: ${formatValue(approxValue)}${unit ? ` ${unit}` : ""}`}>
          <div
            style={{
              fontSize: "10px",
              color: "#c9a971",
              marginBottom: "2px",
              display: "flex",
              justifyContent: "space-between",
            }}
          >
            <span>Approx</span>
            <span>
              {formatValue(approxValue)}
              {unit ? ` ${unit}` : ""}
            </span>
          </div>
          <div
            style={{
              height: "8px",
              borderRadius: "999px",
              background: "rgba(255,255,255,0.08)",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                height: "100%",
                width: approxWidth,
                background: "#f5b84a",
              }}
            />
          </div>
        </div>
      </div>
      {(showError || fasterPercent !== null) && (
        <div
          style={{
            marginTop: "6px",
            padding: "4px 8px",
            background: "rgba(90,170,245,0.08)",
            borderRadius: "4px",
            fontSize: "10px",
            color: "#5aaaf5",
            display: "flex",
            gap: "12px",
            flexWrap: "wrap",
          }}
        >
          {errorPercent !== null && <span>Error: {errorPercent.toFixed(2)}%</span>}
          {fasterPercent !== null && (
            <span>
              Time:{" "}
              {fasterPercent >= 0
                ? `${fasterPercent.toFixed(0)}% faster`
                : `${Math.abs(fasterPercent).toFixed(0)}% slower`}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
