---
name: actions
description: Use when the user asks you to do something outside the chat (remind or notify a colleague) or asks what is waiting for their approval.
metadata:
  include_tools: propose_action my_approvals org_lookup
---
# Actions and approvals

1. Only act when the user asked for it. Resolve the colleague with `org_lookup` if you only have a name.
2. Call `propose_action(kind="send_reminder", target_person_id, title, message)`. The user is asked to approve in
   chat before anything runs. If they reject, say it was not sent and do not try again.
3. After it runs, report it in one line: what was sent and to whom. If it is queued, name who has to approve it.
4. "What needs my approval?" -> `my_approvals()`; one line per item. You cannot decide items yourself: point the
   user to their inbox.
