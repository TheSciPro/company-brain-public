---
name: onboarding
description: Use when a new joiner asks what to do next, about their first weeks, their buddy, or says they finished an onboarding task.
metadata:
  include_tools: onboarding_brief onboarding_step update_onboarding_item company_fact org_lookup
---
# Onboarding a new joiner

The welcome, when it is running, is one beat at a time. The system note's guidance is private: the reply is one
natural line, and the empty `<onboarding-beat id="..."/>` marker is not for the user. Answer the user's question
first. When they answer the beat or skip it, call `onboarding_step`. HR facts are not memories: a correction is
`add_memory(kind="correction")`. How they like answers goes in `/memories/USER.md` with `edit_file`, outside the
directory block.

1. Call `onboarding_brief()` first. Lead with overdue items, then the next 2-3 due soon, each with its due date.
2. Name the manager and buddy from the brief; never invent people.
3. When the user says a task is done, call `update_onboarding_item(item_id, "done")` with the matching id. Items with
   a verifier are checked against company records; if verification fails, say what the record shows.
4. Items that point at a policy: answer with `company_fact` so the user's own scope (country, client) applies.
5. Keep it short: offer more instead of dumping everything.
