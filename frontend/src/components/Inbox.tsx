import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { ActionView, InboxItem } from "../types";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";

export interface Counts {
  approvals: number;
  unread: number;
}

function useInbox(person: string, refreshKey: number, onCounts: (c: Counts) => void) {
  const [approvals, setApprovals] = useState<ActionView[]>([]);
  const [items, setItems] = useState<InboxItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const [a, i] = await Promise.all([api.approvals(person), api.inbox(person)]);
      setApprovals(a);
      setItems(i);
      setError(null);
      onCounts({ approvals: a.length, unread: i.filter((x) => !x.read).length });
    } catch (err) {
      setError(String(err));
    }
  }, [person, onCounts]);
  useEffect(() => {
    void load();
  }, [load, refreshKey]);
  return { approvals, items, error, setError, load };
}

const Empty = ({ text }: { text: string }) => <p className="py-6 text-center text-sm text-slate-500">{text}</p>;
const Failed = ({ text }: { text: string }) => <div className="rounded-md bg-red-50 p-3 text-sm text-red-700">{text}</div>;

export function Approvals({ person, refreshKey, onCounts }: { person: string; refreshKey: number; onCounts: (c: Counts) => void }) {
  const { approvals, error, setError, load } = useInbox(person, refreshKey, onCounts);
  const [notice, setNotice] = useState<string | null>(null);

  async function decide(a: ActionView, decision: "approve" | "reject") {
    if (!a.approval_id) return;
    try {
      const done = await api.decide(person, a.approval_id, decision);
      setNotice(`${decision === "approve" ? "Approved" : "Rejected"}: ${a.title}${done.note ? ` (${done.note})` : ""}`);
    } catch (err) {
      setError(String(err));
    }
    await load();
  }

  return (
    <div className="inbox flex flex-col gap-3">
      {error && <Failed text={error} />}
      {notice && <div className="notice rounded-md bg-emerald-50 p-3 text-sm text-emerald-800">{notice}</div>}
      {approvals.length === 0 && <Empty text="Nothing waiting for you." />}
      {approvals.map((a) => (
        <Card key={a.action_id} className="approval-item">
          <CardHeader>
            <CardTitle className="title">{a.title}</CardTitle>
            <div className="flex gap-1.5">
              <Badge variant="secondary">{a.kind.replace(/_/g, " ")}</Badge>
              <Badge variant="outline">via {a.proposed_by}</Badge>
            </div>
          </CardHeader>
          <CardContent>
            <p className="mb-3 whitespace-pre-wrap leading-relaxed">{String(a.args.message ?? "")}</p>
            <div className="flex gap-2">
              <Button size="sm" onClick={() => decide(a, "approve")}>
                Approve
              </Button>
              <Button size="sm" variant="outline" onClick={() => decide(a, "reject")}>
                Reject
              </Button>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export function Inbox({ person, refreshKey, onCounts }: { person: string; refreshKey: number; onCounts: (c: Counts) => void }) {
  const { items, error, load } = useInbox(person, refreshKey, onCounts);
  return (
    <div className="flex flex-col gap-3">
      {error && <Failed text={error} />}
      {items.length === 0 && <Empty text="No messages." />}
      {items.map((i) => (
        <Card key={i.id} className={i.read ? "opacity-60" : ""}>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Badge variant={i.kind === "approval" ? "tier" : "secondary"}>{i.kind}</Badge>
              {!i.read && <span className="h-2 w-2 rounded-full bg-indigo-500" aria-label="unread" />}
            </div>
            <CardTitle>{i.title}</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="mb-2 whitespace-pre-wrap leading-relaxed">{i.body}</p>
            {!i.read && (
              <Button size="sm" variant="ghost" onClick={() => api.markRead(person, i.id).then(load)}>
                Mark read
              </Button>
            )}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
