import { useMemo } from "react";
import dagre from "dagre";
import ReactFlow, {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  Panel,
  Position,
} from "reactflow";
import type { Edge, Node } from "reactflow";
import "reactflow/dist/style.css";

interface PlanNode {
  type: string;
  columns?: string[];
  aggregates?: string[];
  children?: PlanNode[];
  rows?: number;
  cardinality?: number;
  memory?: string;
  [key: string]: unknown;
}

interface GraphStats {
  totalNodes: number;
  maxDepth: number;
  scanNodes: number;
  joinNodes: number;
  aggregateNodes: number;
  estimatedRows: number;
}

const nodeWidth = 260;
const nodeHeight = 124;

const getNodeColor = (
  type: string,
): { bg: string; border: string; text: string } => {
  const upperType = type.toUpperCase();

  if (upperType.includes("SCAN") || upperType.includes("FILTER") || upperType.includes("READ")) {
    return { bg: "#15324e", border: "#5aaaf5", text: "#cfe7ff" };
  }
  if (upperType.includes("JOIN")) {
    return { bg: "#442230", border: "#f29cab", text: "#ffd6de" };
  }
  if (upperType.includes("AGGREGATE") || upperType.includes("GROUP")) {
    return { bg: "#263d22", border: "#b5da68", text: "#ddf7b0" };
  }
  if (upperType.includes("SORT") || upperType.includes("ORDER")) {
    return { bg: "#43331f", border: "#f6bf63", text: "#ffe6b6" };
  }
  if (upperType.includes("LIMIT")) {
    return { bg: "#2a2d48", border: "#a7b0ff", text: "#dce0ff" };
  }

  return { bg: "#202020", border: "#7e8795", text: "#e5e7eb" };
};

const formatNumber = (value: number) => new Intl.NumberFormat().format(value);

const readNumericMetric = (node: PlanNode, keys: string[]) => {
  for (const key of keys) {
    const raw = node[key];
    if (typeof raw === "number" && Number.isFinite(raw)) return raw;
    if (typeof raw === "string") {
      const parsed = Number(raw);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return null;
};

const collectStats = (plan: PlanNode): GraphStats => {
  const stats: GraphStats = {
    totalNodes: 0,
    maxDepth: 0,
    scanNodes: 0,
    joinNodes: 0,
    aggregateNodes: 0,
    estimatedRows: 0,
  };

  const walk = (node: PlanNode, depth: number) => {
    stats.totalNodes += 1;
    stats.maxDepth = Math.max(stats.maxDepth, depth);

    const type = node.type.toUpperCase();
    if (type.includes("SCAN") || type.includes("READ") || type.includes("FILTER")) {
      stats.scanNodes += 1;
    }
    if (type.includes("JOIN")) {
      stats.joinNodes += 1;
    }
    if (type.includes("AGGREGATE") || type.includes("GROUP")) {
      stats.aggregateNodes += 1;
    }

    const rows = readNumericMetric(node, [
      "rows",
      "plan_rows",
      "actual_rows",
      "cardinality",
    ]);
    if (rows !== null) {
      stats.estimatedRows += rows;
    }

    (node.children ?? []).forEach((child) => walk(child, depth + 1));
  };

  walk(plan, 1);
  return stats;
};

const buildGraph = (plan: PlanNode) => {
  const nodes: Node[] = [];
  const edges: Edge[] = [];
  let idCounter = 1;
  const dagreGraph = new dagre.graphlib.Graph();

  dagreGraph.setDefaultEdgeLabel(() => ({}));
  dagreGraph.setGraph({
    rankdir: "TB",
    nodesep: 42,
    ranksep: 72,
    marginx: 16,
    marginy: 16,
  });

  const walk = (node: PlanNode, parentId: string | null) => {
    const id = `n${idCounter++}`;
    const colors = getNodeColor(node.type);
    const nodeLabel = node.type.replace(/_/g, " ");
    const rows = readNumericMetric(node, [
      "rows",
      "plan_rows",
      "actual_rows",
      "cardinality",
    ]);
    const cost = readNumericMetric(node, ["total_cost", "startup_cost", "cost"]);
    const time = readNumericMetric(node, [
      "actual_total_time",
      "execution_time",
      "time",
    ]);

    const columnPreview =
      node.columns && node.columns.length > 0
        ? node.columns.slice(0, 2).join(", ")
        : null;

    nodes.push({
      id,
      position: { x: 0, y: 0 },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
      data: {
        label: (
          <div style={{ width: "100%" }}>
            <div
              style={{
                fontSize: "11px",
                fontWeight: 700,
                letterSpacing: "0.2px",
                marginBottom: "8px",
                textTransform: "uppercase",
              }}
              title={node.type}
            >
              {nodeLabel}
            </div>
            <div
              style={{
                display: "flex",
                gap: "6px",
                flexWrap: "wrap",
                marginBottom: "6px",
              }}
            >
              {rows !== null && (
                <span
                  title="Estimated/actual rows"
                  style={{
                    fontSize: "10px",
                    background: "rgba(255,255,255,0.12)",
                    borderRadius: "999px",
                    padding: "2px 7px",
                  }}
                >
                  rows {formatNumber(Math.round(rows))}
                </span>
              )}
              {cost !== null && (
                <span
                  title="Estimated cost"
                  style={{
                    fontSize: "10px",
                    background: "rgba(255,255,255,0.12)",
                    borderRadius: "999px",
                    padding: "2px 7px",
                  }}
                >
                  cost {cost.toFixed(1)}
                </span>
              )}
              {time !== null && (
                <span
                  title="Execution time metric"
                  style={{
                    fontSize: "10px",
                    background: "rgba(255,255,255,0.12)",
                    borderRadius: "999px",
                    padding: "2px 7px",
                  }}
                >
                  time {time.toFixed(2)}ms
                </span>
              )}
            </div>
            {(columnPreview || (node.aggregates && node.aggregates.length > 0)) && (
              <div style={{ fontSize: "10px", lineHeight: 1.35, opacity: 0.9 }}>
                {columnPreview && (
                  <div title={node.columns?.join(", ")}>cols: {columnPreview}</div>
                )}
                {node.aggregates && node.aggregates.length > 0 && (
                  <div title={node.aggregates.join(", ")}>
                    agg: {node.aggregates[0]}
                  </div>
                )}
              </div>
            )}
          </div>
        ),
      },
      style: {
        width: nodeWidth,
        minHeight: `${nodeHeight}px`,
        padding: "10px 12px",
        borderRadius: "12px",
        border: `1px solid ${colors.border}`,
        background: `linear-gradient(165deg, ${colors.bg} 0%, #111827 100%)`,
        color: colors.text,
        boxShadow: `0 10px 30px ${colors.border}30`,
      },
    });

    dagreGraph.setNode(id, { width: nodeWidth, height: nodeHeight });

    if (parentId) {
      const edgeId = `e-${parentId}-${id}`;
      edges.push({
        id: edgeId,
        source: parentId,
        target: id,
        type: "smoothstep",
        style: {
          stroke: "rgba(130,170,220,0.45)",
          strokeWidth: 2,
        },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: "rgba(130,170,220,0.55)",
        },
      });
      dagreGraph.setEdge(parentId, id);
    }

    (node.children ?? []).forEach((child) => walk(child, id));
  };

  walk(plan, null);
  dagre.layout(dagreGraph);

  const layoutedNodes = nodes.map((node) => {
    const position = dagreGraph.node(node.id);
    return {
      ...node,
      position: {
        x: position.x - nodeWidth / 2,
        y: position.y - nodeHeight / 2,
      },
    };
  });

  return { nodes: layoutedNodes, edges };
};

function PlanGraph({ plan }: { plan: PlanNode }) {
  const { nodes, edges } = useMemo(() => buildGraph(plan), [plan]);
  const stats = useMemo(() => collectStats(plan), [plan]);

  return (
    <ReactFlow nodes={nodes} edges={edges} fitView>
      <Background color="#1f2937" gap={24} />
      <MiniMap
        style={{ background: "#0d1117", border: "0.5px solid #2f3b4f" }}
        nodeStrokeColor={(node) => String(node.style?.border ?? "#6b7280")}
      />
      <Controls style={{ background: "#0f172a", border: "0.5px solid #334155" }} />
      <Panel
        position="top-left"
        style={{
          background: "rgba(15,23,42,0.82)",
          border: "0.5px solid rgba(148,163,184,0.35)",
          borderRadius: "10px",
          padding: "8px 10px",
          color: "#d1d5db",
          display: "flex",
          gap: "10px",
          flexWrap: "wrap",
          fontSize: "11px",
        }}
      >
        <span>nodes: {stats.totalNodes}</span>
        <span>depth: {stats.maxDepth}</span>
        <span>scans: {stats.scanNodes}</span>
        <span>joins: {stats.joinNodes}</span>
        <span>aggregates: {stats.aggregateNodes}</span>
        <span>rows(sum): {formatNumber(Math.round(stats.estimatedRows))}</span>
      </Panel>
    </ReactFlow>
  );
}

export default PlanGraph;
