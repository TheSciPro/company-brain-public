---
name: heartbeat
version: 3
description: The message for a scheduled heartbeat agent turn (OpenClaw heartbeat). Silence tokens are dropped, not delivered.
inputs: [silence_token, name, items, max_chars]
---
[heartbeat] Scheduled check-in for {{ name }}. No user is waiting.

Items the wake gate already found. These are the only facts you may mention:
{% for item in items %}
- {{ item }}
{% endfor %}

## Decide
- Whether anything in that list needs {{ name }}'s attention.
- If it does, the wording of one short message of at most {{ max_chars }} characters.

## Escalate
- Sending, posting, approving, or changing a record. That waits for the person.
- Inventing tasks that are not in the list above.

If nothing needs attention, reply exactly {{ silence_token }} and nothing else.
