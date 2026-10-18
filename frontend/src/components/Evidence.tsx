import { Background, MarkerType, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { ChevronRight } from "lucide-react";
import { levelLabel, notOpenedReason } from "../labels";
import type { Source } from "../types";
import type { Turn } from "../turn";
import { Badge } from "./ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "./ui/collapsible";

const hopsOf = (query?: string) => (query?.match(/-\[/g) ?? []).length;
const hopsText = (n: number) => (n === 0 ? "1 node read, 0 hops" : `${n} ${n === 1 ? "hop" : "hops"}`);
const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
const CITATION = /\s*(\[[^\]]*\]|【[^】]*】)/g;
// Fixed layout (no fitView): node width, vertical step and left offset, in px at zoom 1.
const NODE_W = 300;
const STEP_Y = 96;
const VIEW = { x: 60, y: 12, zoom: 1 };

/** Graphs this person was not allowed to open for this answer, with the reason in plain words. */
function closedOf(turn: Turn) {
  return turn.notSearched.flatMap((n) => {
    const why = notOpenedReason(n.reason);
    return why ? [{ level: n.source, reason: n.reason, why }] : [];
  });
}

type Tone = "plain" | "ok" | "deny";
const TONES: Record<Tone, string> = {
  plain: "border-slate-300 bg-white",
  ok: "border-emerald-300 bg-emerald-50",
  deny: "border-red-400 bg-red-50 text-red-800",
};

/** The reasoning path, built only from the evidence the run streamed: question, access, graph, nodes, answer. */
function pathOf(turn: Turn): { title: string; detail: string; tone: Tone }[] {
  const closed = closedOf(turn);
  const denied = closed.filter((c) => c.reason !== "unavailable");
  const steps: { title: string; detail: string; tone: Tone }[] = [
    { title: "Question", detail: clip(turn.question ?? "", 60), tone: "plain" },
  ];
  if (turn.searched.length || closed.length)
    steps.push({
      title: "Access check",
      detail: [
        turn.searched.length ? `cleared: ${turn.searched.map(levelLabel).join(", ")}` : "",
        closed.length ? `closed: ${closed.map((c) => levelLabel(c.level)).join(", ")}` : "",
      ]
        .filter(Boolean)
        .join(" · "),
      tone: "ok",
    });
  if (!turn.sources.length && denied.length) {
    steps.push({ title: "Not allowed", detail: denied.map((c) => `${levelLabel(c.level)}: ${c.why}`).join(" · "), tone: "deny" });
    return steps;
  }
  const graphs = [...new Set(turn.sources.flatMap((s) => (s.graph ? [s.graph] : [])))];
  if (graphs.length) steps.push({ title: "FalkorDB graph searched", detail: graphs.join(", "), tone: "plain" });
  const read = turn.sources.slice(0, 3).map((s) => (s.node_label ? `(:${s.node_label}) → ${s.id}` : s.id));
  if (read.length) steps.push({ title: "Nodes read", detail: read.join(" · "), tone: "plain" });
  if (!graphs.length && !read.length && turn.steps.length)
    steps.push({ title: "Steps", detail: turn.steps.join(" · "), tone: "plain" });
  steps.push({ title: "Answer", detail: clip(turn.text.replace(CITATION, "").replace(/\s+/g, " ").trim(), 60), tone: "ok" });
  return steps;
}

function ReasoningGraph({ turn }: { turn: Turn }) {
  const path = pathOf(turn);
  const nodes: Node[] = path.map((p, i) => ({
    id: String(i),
    position: { x: 0, y: i * STEP_Y },
    data: {
      label: (
        <div className="text-left">
          <div className="text-[11px] font-semibold uppercase tracking-wide opacity-70">{p.title}</div>
          <div className="text-xs leading-snug">{p.detail}</div>
        </div>
      ),
    },
    style: { width: NODE_W },
    className: `!rounded-lg !border !px-3 !py-2 !shadow-sm ${TONES[p.tone]}`,
    draggable: false,
    connectable: false,
  }));
  const edges: Edge[] = path.slice(1).map((p, i) => ({
    id: `e${i}`,
    source: String(i),
    target: String(i + 1),
    markerEnd: { type: MarkerType.ArrowClosed },
    style: p.tone === "deny" ? { stroke: "#f87171" } : undefined,
  }));
  return (
    <div className="reasoning-graph rounded-lg border border-slate-200 bg-slate-50" style={{ height: path.length * STEP_Y + 8 }} data-testid="reasoning-graph">
      <ReactFlow nodes={nodes} edges={edges} defaultViewport={VIEW} proOptions={{ hideAttribution: true }} nodesDraggable={false} zoomOnScroll={false} panOnDrag={false}>
        <Background gap={16} color="#e2e8f0" />
      </ReactFlow>
    </div>
  );
}

function SourceCard({ source, n }: { source: Source; n: number }) {
  // The first source shows its Cypher open: the read that backs the answer, visible at a glance.
  const hops = hopsOf(source.query);
  return (
    <Card className="source" id={`evidence-${source.id}`}>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant="secondary">{n}</Badge>
          <Badge variant="tier">{levelLabel(source.level ?? source.kind)}</Badge>
          <code className="text-xs font-semibold text-slate-800">{source.id}</code>
        </div>
        {source.title && source.title !== source.id && <CardTitle>{source.title}</CardTitle>}
      </CardHeader>
      {source.graph && (
        <CardContent className="space-y-1.5">
          <dl className="grid grid-cols-[88px_1fr] gap-x-2 gap-y-1 text-xs">
            <dt className="text-slate-500">FalkorDB graph</dt>
            <dd>
              <code>{source.graph}</code>
            </dd>
            {source.node_label && (
              <>
                <dt className="text-slate-500">Node</dt>
                <dd>
                  <code>{`(:${source.node_label} {id: "${source.node_id ?? source.id}"})`}</code>
                </dd>
              </>
            )}
            {source.query && (
              <>
                <dt className="text-slate-500">Read</dt>
                <dd>{hopsText(hops)}</dd>
              </>
            )}
          </dl>
          {source.query && (
            <Collapsible defaultOpen={n === 1}>
              <CollapsibleTrigger className="group flex items-center gap-1 text-xs font-medium text-slate-600 hover:text-slate-900">
                <ChevronRight className="h-3.5 w-3.5 transition-transform group-data-[state=open]:rotate-90" />
                Cypher query
              </CollapsibleTrigger>
              <CollapsibleContent>
                <pre className="mt-1.5 overflow-x-auto rounded-md bg-slate-900 p-2.5 text-[11px] leading-relaxed text-slate-100">{source.query}</pre>
              </CollapsibleContent>
            </Collapsible>
          )}
        </CardContent>
      )}
    </Card>
  );
}

const EMPTY = "Ask something. The evidence behind each answer shows here.";

/** Everything an answer rests on, for the selected (or latest) answer. Personal memory is not company evidence:
 * an answer that rests only on it shows the empty state. */
export function EvidencePanel({ turn }: { turn: Turn | null }) {
  const memoryOnly = turn?.memory && !turn.sources.length && !turn.searched.length && !turn.notSearched.length;
  if (!turn || memoryOnly) return <p className="py-6 text-center text-sm text-slate-500">{EMPTY}</p>;
  const closed = closedOf(turn);
  return (
    <div className="sources flex flex-col gap-3" data-testid="evidence">
      <div className="text-xs text-slate-500">
        Evidence for <span className="font-medium text-slate-800">“{clip(turn.question ?? "", 80)}”</span>
      </div>
      <ReasoningGraph turn={turn} />
      {turn.sources.map((s, i) => (
        <SourceCard key={s.id} source={s} n={i + 1} />
      ))}
      {!turn.sources.length && <p className="text-sm text-slate-600">No record this person may see.</p>}
      {turn.searched.length > 0 && (
        <div className="text-xs text-slate-600">
          Graphs searched:{" "}
          {turn.searched.map((g) => (
            <Badge key={g} variant="outline" className="mr-1">
              {levelLabel(g)}
            </Badge>
          ))}
        </div>
      )}
      {closed.map((c) => (
        <div key={c.level} className="not-searched text-xs text-red-700">
          <Badge variant="danger" className="mr-1">
            not opened
          </Badge>
          {levelLabel(c.level)} ({c.why})
        </div>
      ))}
    </div>
  );
}
