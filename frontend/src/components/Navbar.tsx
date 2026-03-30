import { Link, useLocation } from "react-router-dom";

export default function Navbar() {
  const location = useLocation();

  return (
    <nav
      style={{
        display: "flex",
        alignItems: "center",
        gap: "24px",
        padding: "12px 28px",
        background: "#0f0f0f",
        borderBottom: "0.5px solid rgba(255,255,255,0.08)",
        position: "sticky",
        top: 0,
        zIndex: 100,
      }}
    >
      <h2
        style={{
          margin: 0,
          fontSize: "16px",
          fontWeight: 700,
          letterSpacing: "-0.3px",
          color: "#e0ddd8",
        }}
      >
        AetherQuery
      </h2>

      <div style={{ display: "flex", gap: "16px" }}>
        <Link
          to="/"
          style={{
            fontSize: "13px",
            fontWeight: 600,
            padding: "6px 12px",
            borderRadius: "6px",
            transition: "all 0.15s",
            textDecoration: "none",
            color:
              location.pathname === "/" ? "#ffffff" : "rgba(224,221,216,0.6)",
            background:
              location.pathname === "/"
                ? "rgba(24,95,165,0.15)"
                : "transparent",
            border:
              location.pathname === "/"
                ? "0.5px solid rgba(24,95,165,0.3)"
                : "0.5px solid transparent",
          }}
        >
          Executor
        </Link>

        <Link
          to="/plan"
          style={{
            fontSize: "13px",
            fontWeight: 600,
            padding: "6px 12px",
            borderRadius: "6px",
            transition: "all 0.15s",
            textDecoration: "none",
            color:
              location.pathname === "/plan"
                ? "#ffffff"
                : "rgba(224,221,216,0.6)",
            background:
              location.pathname === "/plan"
                ? "rgba(24,95,165,0.15)"
                : "transparent",
            border:
              location.pathname === "/plan"
                ? "0.5px solid rgba(24,95,165,0.3)"
                : "0.5px solid transparent",
          }}
        >
          Plan Analyzer
        </Link>
      </div>

      <div style={{ marginLeft: "auto" }}>
        <span style={{ fontSize: "11px", color: "#666" }}>
          ✓ Connected to backend
        </span>
      </div>
    </nav>
  );
}
