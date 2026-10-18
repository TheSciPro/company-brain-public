---
name: memory-hygiene
description: Use when the user asks what you remember, corrects something you know, asks where a memory came from, or tells you how they like to work.
---
# Memory hygiene

Two homes, both under `/memories/` and both reached with the built-in file tools:

- `/memories/USER.md` — how this person likes answers (length, format, language, tone).
- `/memories/facts/` — dated work facts. Each file has `valid_at`, `invalid_at`, `supersedes` and `superseded_by`.
  Current facts are the ones with an empty `invalid_at`. Facts are append-only.

1. "What do you remember about X?" -> `grep` X under `/memories/facts` and read USER.md. If `<runtime>` says
   `not_searched: graphiti`, say you did not search dated facts. If nothing matches, say you do not know.
2. "Where did that come from?" -> `read_file` the fact id from the grep hit. Quote `valid_at` and the text.
3. A correction -> `write_file` a new file under `/memories/facts/` with the corrected fact. Do not edit the
   old file. The previous fact stays, with `invalid_at` set when the new one supersedes it.
4. How the user wants answers -> `edit_file` on `/memories/USER.md` under "How they like answers". Read the
   file first; keep it short; replace placeholders. Do not write preferences into `/memories/facts/`.
5. Never claim you forgot or deleted something. A fact file cannot be deleted.
