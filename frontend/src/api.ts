// Typed client for the FastAPI app. Identity is the demo header (X-NWB-Person); real auth replaces it server-side.
import { readSse } from "./sse";
import type { ActionView, ChatEvent, InboxItem, Persona } from "./types";

const BASE = "/api";
const HEADER = "X-NWB-Person";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`${res.status}: ${detail}`);
  }
  return (await res.json()) as T;
}

function headers(person: string): HeadersInit {
  return { "Content-Type": "application/json", [HEADER]: person };
}

export const api = {
  personas: () => fetch(`${BASE}/personas`).then((r) => json<Persona[]>(r)),
  inbox: (person: string) => fetch(`${BASE}/inbox`, { headers: headers(person) }).then((r) => json<InboxItem[]>(r)),
  markRead: (person: string, id: string) =>
    fetch(`${BASE}/inbox/${id}/read`, { method: "POST", headers: headers(person) }).then((r) => json<unknown>(r)),
  approvals: (person: string) =>
    fetch(`${BASE}/approvals`, { headers: headers(person) }).then((r) => json<ActionView[]>(r)),
  decide: (person: string, approvalId: string, decision: "approve" | "reject") =>
    fetch(`${BASE}/approvals/${approvalId}/decide`, {
      method: "POST",
      headers: headers(person),
      body: JSON.stringify({ decision }),
    }).then((r) => json<ActionView>(r)),
  endSession: (person: string, sessionId: string) =>
    fetch(`${BASE}/sessions/${sessionId}/end`, { method: "POST", headers: headers(person) }).then((r) =>
      json<unknown>(r),
    ),
};

async function* stream(person: string, path: string, body: unknown): AsyncGenerator<ChatEvent> {
  const res = await fetch(`${BASE}${path}`, { method: "POST", headers: headers(person), body: JSON.stringify(body) });
  if (!res.ok || !res.body) throw new Error(`${res.status}: ${await res.text()}`);
  for await (const raw of readSse(res.body)) {
    yield { event: raw.event, data: JSON.parse(raw.data) } as ChatEvent;
  }
}

export const chatStream = (person: string, message: string, sessionId: string | null) =>
  stream(person, "/chat", { message, session_id: sessionId });

export type Decision = "approve" | "reject";

/** One decision per pending action request, in order (LangGraph HITL contract). */
export const resumeStream = (person: string, runId: string, decisions: Decision[]) =>
  stream(person, `/runs/${runId}/resume`, { decisions: decisions.map((type) => ({ type })) });
