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
        background: "rgba(15,15,15,0.78)",
        borderBottom: "0.5px solid rgba(255,255,255,0.08)",
        backdropFilter: "blur(14px)",
        position: "sticky",
        top: 0,
        zIndex: 100,
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "2px" }}>
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
        <span style={{ fontSize: "10px", color: "#7c8794", letterSpacing: "0.4px" }}>
          Approximate Analytics Engine
        </span>
      </div>

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
        <span style={{ fontSize: "11px", color: "#7caee0" }}>
          Runtime sampling enabled
        </span>
      </div>
    </nav>
  );
}
