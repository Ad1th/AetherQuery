export default function Spinner({ size = 16 }: { size?: number }) {
  return (
    <div
      style={{
        display: "inline-block",
        width: size,
        height: size,
        border: `2px solid rgba(255,255,255,0.2)`,
        borderTop: `2px solid #5aaaf5`,
        borderRadius: "50%",
        animation: "spin 0.8s linear infinite",
      }}
    >
      <style>{`
        @keyframes spin {
          0% { transform: rotate(0deg); }
          100% { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}
