import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { Chat } from "./components/Chat";
import { EvidencePanel } from "./components/Evidence";
import { Approvals, Inbox, type Counts } from "./components/Inbox";
import { PersonaSwitcher } from "./components/PersonaSwitcher";
import { Badge } from "./components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./components/ui/tabs";
import type { Turn } from "./turn";
import type { Persona } from "./types";

export function App() {
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [person, setPerson] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<Turn | null>(null);
  const [tab, setTab] = useState("evidence");
  const [counts, setCounts] = useState<Counts>({ approvals: 0, unread: 0 });
  const onCounts = useCallback((c: Counts) => setCounts((old) => ({ ...old, ...c })), []);
  const onEvidence = useCallback((t: Turn | null) => setEvidence(t), []);

  useEffect(() => {
    api
      .personas()
      .then(setPersonas)
      .catch((err) => setError(`API not reachable (${err}). Start it with: nwb serve`));
  }, []);

  return (
    <div className="app flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-3">
        <div className="flex items-center gap-2.5">
          <div className="grid h-8 w-8 place-items-center rounded-lg bg-slate-900 text-sm font-bold text-white">N</div>
          <div className="leading-tight">
            <h1 className="text-base font-semibold">Northwind Brain</h1>
            <div className="text-xs text-slate-500">Answers from FalkorDB, scoped to who you are</div>
          </div>
        </div>
        {personas.length > 0 && (
          <PersonaSwitcher
            personas={personas}
            value={person}
            onChange={(id) => {
              setEvidence(null);
              setPerson(id);
            }}
          />
        )}
      </header>
      {error && <div className="error m-4 rounded-md bg-red-50 p-3 text-sm text-red-700">{error}</div>}
      {!person && personas.length > 0 && (
        <p className="hint py-24 text-center text-slate-500">Choose who you are, top right. Each person sees only what they may see.</p>
      )}
      {person && (
        <main className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_440px] gap-6 px-6 py-4">
          {/* key = person: switching persona starts a fresh chat (sessions are per person) */}
          <Chat key={person} person={person} onActivity={() => setRefresh((n) => n + 1)} onEvidence={onEvidence} />
          <Tabs value={tab} onValueChange={setTab} className="flex min-h-0 flex-col">
            <TabsList className="self-start">
              <TabsTrigger value="evidence" data-testid="tab-evidence">
                Evidence
              </TabsTrigger>
              <TabsTrigger value="approvals" data-testid="tab-approvals">
                Approvals {counts.approvals > 0 && <Badge>{counts.approvals}</Badge>}
              </TabsTrigger>
              <TabsTrigger value="inbox" data-testid="tab-inbox">
                Inbox {counts.unread > 0 && <Badge variant="tier">{counts.unread}</Badge>}
              </TabsTrigger>
            </TabsList>
            <div className="min-h-0 flex-1 overflow-y-auto pr-1">
              <TabsContent value="evidence">
                <EvidencePanel turn={evidence} />
              </TabsContent>
              <TabsContent value="approvals" forceMount className="data-[state=inactive]:hidden">
                <Approvals person={person} refreshKey={refresh} onCounts={onCounts} />
              </TabsContent>
              <TabsContent value="inbox" forceMount className="data-[state=inactive]:hidden">
                <Inbox person={person} refreshKey={refresh} onCounts={onCounts} />
              </TabsContent>
            </div>
          </Tabs>
        </main>
      )}
    </div>
  );
}
