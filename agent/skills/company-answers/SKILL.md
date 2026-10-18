---
name: company-answers
description: Use for any question about company rules, limits, numbers, documents, people or who approves something.
metadata:
  include_tools: company_fact fact_history search_company who_approves org_lookup
---
# Answering company questions

The tools already know who the user is, their country and client assignments, and today's date.

1. A rule, limit or number -> call `company_fact(question)` once, with the user's words. Pass `client` only if the user names one. Then stop. Do not delegate that question to the researcher.
2. If the result has `say`, reply with that sentence and nothing else. Do not call `search_company`, `fact_history`, `read_file`, or `company_fact` again.
3. If `say` or `note` says the fact is not on record, tell the user that and who to ask. Do not substitute a different fact. Do not mention counts, hidden sources, or that more matches exist.
4. Never answer from a candidate whose key is about something else. A visible fact on a different subject is not the answer.
5. Approval questions -> `who_approves(action, amount)` once. Documents that are not a rule or number -> `search_company(query)` once. People -> `org_lookup(who)` once.
6. Cite only ids that appear in the tool result from this turn. `search_company` lists `not_searched` sources; you may name those sources as not searched, and nothing more.
7. "How did this change?" after an answer -> `fact_history(key)` once, then stop.
