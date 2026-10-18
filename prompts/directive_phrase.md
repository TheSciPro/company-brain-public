---
name: directive_phrase
version: 3
description: >
  Phrase one preference from a deterministic edit diff. Keep every concrete word
  the edit added or replaced. Do not generalise that change into a vaguer preference.
inputs: [tool, kind, field, before, after, max_chars]
---
A person edited one field of a drafted action before approving it. The difference is already known.
The preference is that difference, kept concrete. Do not generalise it.
Before and after are only the words that changed, not the unchanged wording around them.

Tool: {{ tool }}
Action kind: {{ kind }}
Field: {{ field }}
Before: {{ before }}
After: {{ after }}

Reply with a single JSON object {"text": "..."} and nothing else.
"text" is one sentence, at most {{ max_chars }} characters.
Repeat every concrete word that is in the after text and not in the before text: names, counts, deadlines, and the wording that was added or replaced.
Do not replace those words with a vaguer phrase, and do not drop them.
Do not start the sentence with Always, Never or Prefer. Do not include a date. Use only the before and after text.
