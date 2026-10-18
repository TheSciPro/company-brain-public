from common import ROOT
import json
from questions import Q, MEMORY_SCRIPT
def run():
    rows=[]; md=["# Northwind Company Brain — Demo Question Bank (final)\n",
      "Demo date 2026-09-28 (IST). Each question: persona & role, ask date, exact question, expected answer, supporting records, what must be hidden, traps (spec §9). ",
      "`DOC#n` = clause n of DOC. `HRIS:<id>@<date>` = Workday job event for that worker on that date. Validated by `generator/check_questions.py`.\n",
      "Priority problems: **1 Permission-safe answers · 2 Which rule applies · 3 Decision trail · 4 Who really knows · 5 Memory**; supporting: 6 Approvals · 7 Audit · 8 Staffing · 9 Onboarding & handover.\n"]
    grp=None
    for (qid,g,(who,role),date,q,ans,ev,hide,traps,kind) in Q:
        if g!=grp: md.append(f"\n## {g}\n"); grp=g
        md.append(f"### {qid} — {kind}\n- **Asked by:** {who} — *{role}*, on {date}\n- **Question:** \"{q}\"\n- **Expected:** {ans}\n- **Evidence:** {', '.join('`'+e+'`' for e in ev)}\n- **Must hide:** {hide or '—'}\n" + (f"- **Traps:** {', '.join(traps)}\n" if traps else ""))
        rows.append({"id":qid,"group":g,"persona":who,"role":role,"ask_date":date,"question":q,"expected":ans,"evidence":ev,"must_hide":hide,"traps":traps,"kind":kind})
    md.append("\n## Memory script\n"+MEMORY_SCRIPT)
    (ROOT/"demo_questions.md").write_text("\n".join(md))
    (ROOT/"demo_questions.json").write_text(json.dumps(rows,indent=2,ensure_ascii=False))
    return {"questions":len(rows)}
