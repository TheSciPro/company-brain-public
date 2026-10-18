import { Check, Loader2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, chatStream, resumeStream, type Decision } from "../api";
import { MEMORY_PREFIX, stepLabel } from "../labels";
import type { Turn } from "../turn";
import type { ActionRequest, ChatEvent } from "../types";
import { Answer } from "./Answer";
import { Button } from "./ui/button";
import { Card } from "./ui/card";

const brainTurn = (question?: string, step?: string): Turn => ({
  role: "brain",
  text: "",
  question,
  step,
  steps: step ? [step] : [],
  sources: [],
  searched: [],
  notSearched: [],
  memory: false,
});

const union = <T,>(a: T[], b: T[] | undefined, key: (x: T) => string): T[] => {
  const seen = new Set(a.map(key));
  return [...a, ...(b ?? []).filter((x) => !seen.has(key(x)) && seen.add(key(x)))];
};

/** Shown while a turn runs: the steps so far in plain words, the current one spinning, and elapsed seconds. */
function Working({ steps, streaming }: { steps: string[]; streaming: boolean }) {
  const [started] = useState(() => Date.now());
  const [now, setNow] = useState(started);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const secs = Math.round((now - started) / 1000);
  const current = streaming ? "Writing the answer" : (steps[steps.length - 1] ?? "Understanding the question");
  const done = streaming ? steps : steps.slice(0, -1);
  return (
    <div className="working mb-2 space-y-1 text-sm text-slate-500" role="status">
      {done.map((s, i) => (
        <div key={i} className="flex items-center gap-2">
          <Check className="h-3.5 w-3.5 text-emerald-600" />
          <span className="first-letter:uppercase">{s}</span>
        </div>
      ))}
      <div className="flex items-center gap-2 text-slate-700">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        <span className="first-letter:uppercase">{current}</span>
        <span className="text-xs tabular-nums text-slate-400">{secs} s</span>
      </div>
    </div>
  );
}

interface Pending {
  runId: string;
  requests: ActionRequest[];
}

interface Props {
  person: string;
  onActivity: () => void;
  /** The answer whose evidence the right column shows: the one clicked, else the latest. */
  onEvidence: (turn: Turn | null) => void;
}

export function Chat({ person, onActivity, onEvidence }: Props) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const latest = turns.map((t, i) => (t.role === "brain" && t.question ? i : -1)).filter((i) => i >= 0).pop();
    const i = selected ?? latest;
    onEvidence(i != null ? turns[i] : null);
  }, [turns, selected, onEvidence]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns.length, busy]);

  const patchLast = (fn: (t: Turn) => Turn) => setTurns((ts) => [...ts.slice(0, -1), fn(ts[ts.length - 1])]);

  async function consume(events: AsyncGenerator<ChatEvent>) {
    setBusy(true);
    try {
      for await (const ev of events) {
        switch (ev.event) {
          case "run_started":
            setSessionId(ev.data.session_id);
            break;
          case "token":
            patchLast((t) => ({ ...t, text: t.text + ev.data.text }));
            break;
          case "tool_start": {
            const step = stepLabel(ev.data.tool, ev.data.args);
            const path = typeof ev.data.args?.file_path === "string" ? ev.data.args.file_path : "";
            patchLast((t) => ({
              ...t,
              step,
              steps: t.steps[t.steps.length - 1] === step ? t.steps : [...t.steps, step],
              memory: t.memory || path.startsWith(MEMORY_PREFIX),
            }));
            break;
          }
          case "tool_end": {
            const d = ev.data;
            patchLast((t) => ({
              ...t,
              sources: union(t.sources, d.sources, (x) => x.id),
              searched: union(t.searched, d.searched, (x) => x),
              notSearched: union(t.notSearched, d.not_searched, (x) => x.source),
            }));
            break;
          }
          case "interrupt":
            setPending({ runId: ev.data.run_id, requests: ev.data.interrupts.flatMap((i) => i.action_requests ?? []) });
            break;
          case "final": {
            // The final answer is authoritative (scrubbed server-side); streamed tokens are a preview.
            const answer = ev.data.answer;
            const secs = ev.data.ms != null ? `${(ev.data.ms / 1000).toFixed(1)} s` : "";
            patchLast((t) => ({ ...t, text: answer || t.text, step: undefined, status: secs ? `answered in ${secs}` : "" }));
            break;
          }
          case "error":
            patchLast((t) => ({ ...t, text: t.text || ev.data.message, step: undefined, status: "error" }));
            break;
        }
      }
    } catch (err) {
      patchLast((t) => ({ ...t, text: String(err), status: "error" }));
    } finally {
      setBusy(false);
      onActivity();
    }
  }

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setSelected(null);
    setTurns((ts) => [...ts, { ...brainTurn(), role: "user", text: message }, brainTurn(message)]);
    await consume(chatStream(person, message, sessionId));
  }

  async function decide(decision: Decision) {
    if (!pending) return;
    const { runId, requests } = pending;
    setPending(null);
    setTurns((ts) => [...ts, brainTurn(undefined, decision === "approve" ? "approved, carrying on" : "rejected, carrying on")]);
    await consume(resumeStream(person, runId, requests.map(() => decision)));
  }

  /** End the session so its notes and memories are written; the next message starts a new session. */
  async function endSession() {
    if (!sessionId || busy) return;
    setBusy(true);
    try {
      await api.endSession(person, sessionId);
      setSessionId(null);
      setTurns((ts) => [...ts, { ...brainTurn(), text: "Session ended. Notes written; the next message starts a new session." }]);
    } catch (err) {
      setTurns((ts) => [...ts, { ...brainTurn(), text: String(err), status: "error" }]);
    } finally {
      setBusy(false);
      onActivity();
    }
  }

  return (
    <section className="chat mx-auto flex min-h-0 w-full max-w-4xl flex-1 flex-col">
      <div className="turns flex-1 space-y-4 overflow-y-auto px-1 pb-4">
        {turns.length === 0 && (
          <div className="py-16 text-center text-slate-500">
            <div className="text-base font-medium text-slate-700">Ask the company brain</div>
            <div className="text-sm">Policies, approvals, decisions, your onboarding. Every answer shows its evidence.</div>
          </div>
        )}
        {turns.map((t, i) =>
          t.role === "user" ? (
            <div key={i} className="turn user flex justify-end">
              <div className="text max-w-[80%] rounded-2xl rounded-br-md bg-slate-900 px-4 py-2.5 text-[15px] leading-relaxed text-white">{t.text}</div>
            </div>
          ) : (
            <div key={i} className="turn brain flex justify-start">
              <Card
                onClick={() => t.question && setSelected(i)}
                className={`max-w-[85%] cursor-pointer rounded-2xl rounded-bl-md px-4 py-3 text-[15px] transition-shadow hover:shadow ${
                  t.question && (selected ?? -1) === i ? "ring-2 ring-indigo-300" : ""
                }`}
              >
                {busy && i === turns.length - 1 && !t.status ? <Working steps={t.steps} streaming={t.text.length > 0} /> : null}
                <Answer text={t.text} onCite={() => setSelected(i)} />
                {t.status && (
                  <div className="status mt-2 flex items-center gap-2 text-xs text-slate-400">
                    <span>{t.status}</span>
                    {t.question && (t.sources.length > 0 || t.notSearched.length > 0) && (
                      <span className="text-indigo-600">· {t.sources.length || ""} evidence →</span>
                    )}
                  </div>
                )}
              </Card>
            </div>
          ),
        )}
        <div ref={bottom} />
      </div>
      {pending && (
        <Card className="approval-card mb-3 border-amber-300 bg-amber-50 p-4">
          <div className="mb-2 text-sm font-semibold">Approval needed</div>
          {pending.requests.map((r, i) => (
            <pre key={i} className="mb-2 overflow-x-auto rounded-md bg-white p-2 text-xs">
              {r.name} {JSON.stringify(r.args, null, 2)}
            </pre>
          ))}
          <div className="flex gap-2">
            <Button size="sm" onClick={() => decide("approve")}>
              Approve
            </Button>
            <Button size="sm" variant="outline" onClick={() => decide("reject")}>
              Reject
            </Button>
          </div>
        </Card>
      )}
      <form
        className="composer flex items-center gap-2 rounded-xl border border-slate-200 bg-white p-2 shadow-sm"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about policies, approvals, your onboarding…"
          disabled={busy || pending !== null}
          className="h-9 flex-1 bg-transparent px-2 text-[15px] outline-none placeholder:text-slate-400"
        />
        <Button type="submit" disabled={busy || pending !== null}>
          Send
        </Button>
        <Button type="button" variant="outline" onClick={() => void endSession()} disabled={!sessionId || busy}>
          End session
        </Button>
      </form>
    </section>
  );
}
