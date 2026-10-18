// Wire types mirrored from src/nwb/api/schemas.py and src/nwb/actions/models.py. Keep in sync with the API.

export interface Persona {
  id: string;
  label: string;
  asof: string | null;
}

export interface InboxItem {
  id: string;
  kind: "nudge" | "approval" | "system";
  title: string;
  body: string;
  ref: string | null;
  created_at: string;
  read: boolean;
}

export interface ActionView {
  action_id: string;
  approval_id: string | null;
  kind: string;
  title: string;
  status: string;
  requester: string;
  approver: string | null;
  target: string | null;
  args: Record<string, unknown>;
  proposed_by: string;
  note: string | null;
}

export interface ActionRequest {
  name: string;
  args: Record<string, unknown>;
  description?: string;
}

/** A source a tool answer rests on, with the classification graph (level) it was read from. */
export interface Source {
  id: string;
  kind: string;
  title: string | null;
  level: string | null;
  /** FalkorDB evidence: the graph, the node and the query that returned this source. */
  graph?: string;
  node_id?: string;
  node_label?: string;
  query?: string;
}

/** A classification graph the tool did not open, and why (never a count). */
export interface NotSearched {
  source: string;
  reason: string;
}

/** One server-sent event from POST /chat or POST /runs/{id}/resume. */
export type ChatEvent =
  | { event: "run_started"; data: { run_id: string; session_id: string; asof: string } }
  | { event: "token"; data: { run_id: string; text: string } }
  | { event: "tool_start"; data: { run_id: string; tool: string; args?: Record<string, unknown> } }
  | {
      event: "tool_end";
      data: {
        run_id: string;
        tool: string;
        status: string;
        sources?: Source[];
        searched?: string[];
        not_searched?: NotSearched[];
      };
    }
  | { event: "receipt"; data: { run_id: string; event?: string; memory_id?: string } }
  | { event: "interrupt"; data: { run_id: string; interrupts: { action_requests?: ActionRequest[] }[] } }
  | { event: "final"; data: { run_id: string; status: string; answer?: string; ms?: number } }
  | { event: "error"; data: { run_id: string; message: string } };
