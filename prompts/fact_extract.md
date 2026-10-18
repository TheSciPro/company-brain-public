---
name: fact_extract
version: 2
description: Ingest-time fact extraction from one document. Output is schema-validated and quote-checked.
inputs: [title, doc_type, status, client, body, known_keys, max_facts]
---
You extract normative facts from ONE company document so employees can later ask "what is the rule / number / limit".

Document: {{ title }} (type: {{ doc_type or "-" }}, status: {{ status or "-" }}{% if client %}, about client {{ client }}{% endif %})

Extract every rule that carries a value: limits, quotas, counts, durations, deadlines, notice periods, percentages,
amounts, thresholds, permissions. At most {{ max_facts }} facts. Skip narrative, history, opinions and one-off dates.

For each fact return:
- key: snake_case name of the ATTRIBUTE, generic enough to be shared by other documents on the same subject
  (describe the attribute, never a client, person, document id or date).
  Reuse a known key below whenever it is the same attribute, even if this document words it differently.
  If this document forbids or allows something a known key counts, use that known key with a number
  ("0" when it is forbidden) instead of inventing a yes/no key.
- label: a short human description of the attribute (e.g. "Maximum X per Y").
- value: the value only, normalised: a plain number without unit ("3", "0.5", "72"), or a short token
  ("yes", "no"). A prohibition of something countable is "0".
- unit: the unit if any ("days/week", "hours", "%", "days/year", "working days", "INR", ...), else null.
- country: ONLY when the document itself gives this rule for a specific country and a different rule (or none) for
  others: its ISO 3166-1 alpha-2 code (India -> IN, United Kingdom -> GB). If one sentence gives the same value for
  several named countries, emit one fact per country. A document that merely follows one country's law, or
  covers one client, is NOT country-specific: use null.
- quote: the exact words from the document that state THIS attribute and value (verbatim, 5-200 characters).
  If no sentence states it, do not emit the fact.

Known keys (reuse when the attribute is the same):
{% for k in known_keys %}
- {{ k.key }}: {{ k.label }}{% if k.unit %} [{{ k.unit }}]{% endif %}

{% else %}
(none yet)
{% endfor %}

Document text:
<<<
{{ body }}
>>>
