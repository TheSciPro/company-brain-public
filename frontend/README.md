# Northwind Brain — thin frontend (R6)

Vite + React + TypeScript. Covers three things: streaming chat (SSE over `fetch`, since `EventSource` cannot POST),
a persona switcher (`GET /personas` sets the `X-NWB-Person` demo header), and the inbox with the approval queue
(`GET /approvals`, `POST /approvals/{id}/decide`). In-chat HITL interrupts render as an approval card and resume
the same run through `POST /runs/{run_id}/resume`.

```bash
nwb serve                      # API on :8000 (add --scheduler for heartbeat + watcher)
cd frontend && npm install
npm run dev                    # http://localhost:5173, proxies /api -> NWB_API (default http://127.0.0.1:8000)
npm run typecheck && npm test  # tsc strict + vitest (SSE parser)
```

| File | Role |
|---|---|
| `src/api.ts` | typed client; the only place that knows routes and the identity header |
| `src/sse.ts` | SSE framing parser (unit-tested in `sse.test.ts`) |
| `src/types.ts` | wire types mirrored from `src/nwb/api/schemas.py` |
| `src/components/Chat.tsx` | token streaming, tool trail, interrupt approval card |
| `src/components/Inbox.tsx` | approvals to decide + nudges/system messages |
| `src/components/PersonaSwitcher.tsx` | demo identity switch (not auth) |
