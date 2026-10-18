---
name: runtime_context
version: 6
description: Per-run Memorae v3 runtime block (temporal, account, access, guessed memories). No history, no profile.
inputs: [temporal, account, access, guessed, not_searched, purpose]
---
<runtime purpose="{{ purpose }}">
<temporal>
today: {{ temporal.asof }} ({{ temporal.weekday }}) {{ temporal.time }} {{ temporal.tz }} | tools already use this date; pass on_date only for a different day
{% if temporal.next_days %}next_days: {{ temporal.next_days | join(" | ") }}
{% endif %}
relative: tomorrow={{ temporal.tomorrow }}, next Monday={{ temporal.next_monday }}, end of month={{ temporal.end_of_month }}
</temporal>
<account>
user_id: {{ account.person_id }} | name: {{ account.name }} | title: {{ account.title or "-" }} | grade: {{ account.grade or "-" }} | location: {{ account.location or "-" }} ({{ account.country or "-" }})
company: {{ account.company }} | units: {{ account.units | join(", ") or "-" }} | manager: {{ account.manager or "-" }}
</account>
{% if access %}
<access computed="db at {{ temporal.asof }}">
{{ access }}
</access>
{% endif %}
<guessed_memories note="pre-retrieved by similarity; may be wrong. Verify with grep or read_file on /memories/facts before relying.">
{% for g in guessed %}
[{{ g.id }} {{ "%.2f"|format(g.score) }} {{ g.kind }} {{ g.date }}] {{ g.text }}
{% else %}
(none)
{% endfor %}
{% if not_searched %}
not_searched: {{ not_searched | join(", ") }}
{% endif %}
</guessed_memories>
</runtime>
