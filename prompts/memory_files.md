---
name: memory_files
version: 2
description: Wrapper for deepagents MemoryMiddleware (AGENTS.md, USER.md, MEMORY.md). Short on purpose (TPM budget).
inputs: []
---
<memory_files note="loaded at session start; edits apply from the next session">
{agent_memory}
</memory_files>
Keep /memories/USER.md and /memories/MEMORY.md short and true. One fact, one store. Directives
(Always, Never or Prefer, each with `observed: YYYY-MM-DD | status: active` or superseded) live only in
USER.md. Dated work facts are new files under /memories/facts/ (do not edit an old one, and do not copy the
sentence into USER.md). MEMORY.md may list the date and the fact path. Never store secrets.
If a write to USER.md or MEMORY.md is rejected, do not save that text and do not retry it.
These files are internal: never name or cite a file or path to the user.
