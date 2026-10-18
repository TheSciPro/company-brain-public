---
name: heartbeat
description: Use on a scheduled heartbeat run (purpose=heartbeat) to decide whether anything is worth telling the user.
metadata:
  include_tools: onboarding_brief my_approvals
  scheduled: true
---
# Heartbeat

You are running without a user message (OpenClaw heartbeat). Check, in order, only what applies to this user:

1. `onboarding_brief(focus="overdue")` if they are onboarding.
2. `my_approvals()` for anything waiting on them.
3. Anything in `/memories/MEMORY.md` or a current file under `/memories/facts/` with a date that is now due.

## Decide
- Whether any of those checks has something new that needs this user's attention.
- The wording of one short message (at most 3 bullets) when it does, with the date on each item.

## Escalate
- Sending, posting, approving, or changing a record. That waits for the person.
- Inventing tasks from past chats, or from anything those checks did not return.

If nothing needs their attention, reply with only the silence token you were given, and nothing else.
Never repeat a nudge they already acknowledged.
