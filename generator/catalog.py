"""Builds ../DATA_CATALOG.md with per-folder files, record counts, formats, and questions served (from evidence refs)."""
import json, csv, re
from collections import defaultdict
from common import DATA, ROOT
import io, contextlib
with contextlib.redirect_stdout(io.StringIO()):
    import check_questions as cq
DESC={"directory":("Okta / Entra ID","SCIM 2.0 Users (core + enterprise extension + Northwind HR ext) & Groups; ACL classification map. Implicitly used by every question (identity, grade, manager, group-based ACL evaluation)"),
"hris":("Workday","Effective-dated job events (JSON+CSV), worker snapshot, skills/certs, restricted HR case stubs, ORG-2026-05 memo"),
"policies":("SharePoint / Confluence","Markdown + YAML front-matter (doc_id, version, status, effective dates, supersedes, owner, approver, scope, classification, acl); DoA matrix CSV"),
"contracts":("Icertis CLM","MSA/SOW/DPA markdown with clause numbering; obligations.json; client escalations"),
"approvals":("SAP Concur / Salesforce Deal Desk / Coupa / ServiceNow / Workday Absence","Approval records with steps[] (approver, action, ts, comment, delegation); pricing-exception register; background noise JSONL"),
"decisions":("Confluence decision log","Nygard-style ADR/RFC markdown (status, supersedes/superseded_by, governing policy, evidence)"),
"engineering":("GitHub Enterprise / Jira Cloud","Repos, CODEOWNERS files + history, PRs with reviews, commits, runbook blame, Jira issues incl. RAID, activity event stream"),
"collab":("Slack / Teams / M365 Calendar","Slack-export-style threads, meeting minutes (decisions + actions only), calendar metadata"),
"staffing":("Kantata PSA","Allocations (hard/soft, %, dates), demand & bench-pull records, milestones"),
"security_grc":("Vanta / Okta System Log / ServiceNow SecOps / Intune / KnowBe4","ISO 27001:2022 controls, evidence, Okta System Log events, incident+SEC+PIR, audit findings, access-review schedule, ITSM tickets, devices, training")}
def count(p):
    if p.suffix==".json":
        o=json.loads(p.read_text()); 
        if isinstance(o,dict) and "Resources" in o: return len(o["Resources"])
        return len(o) if isinstance(o,list) else 1
    if p.suffix==".jsonl": return sum(1 for _ in p.open())
    if p.suffix==".csv": return sum(1 for _ in csv.DictReader(p.open()))
    return 1
qs=json.loads((ROOT/"demo_questions.json").read_text())
serve=defaultdict(set)
for q in qs:
    for e in q["evidence"]:
        d=e.split("#")[0]
        f="hris/job_events.json" if e.startswith("HRIS:") else cq.idx.get(d)
        if f: serve[f.split("/")[0]].add(q["id"])
md=["# DATA_CATALOG — Northwind synthetic source data\n","Regenerate structured data: `cd generator && python3 run_all.py && python3 check_questions.py && python3 catalog.py` (seed `NORTHWIND_SEED=2026`). Markdown documents in policies/, contracts/, decisions/, collab/minutes/ and hris/ORG-2026-05.md are hand-composed and not overwritten.\n",
"Every record carries `id`, `source_system`, `created_at`/`updated_at`, `classification`, `acl` (groups/users) — at top level, in `x_northwind` (SCIM/Okta), or in YAML front-matter (markdown). Field-level restrictions are in `restricted_fields[]` (`field`, `classification`, `acl`, `existence_disclosure`). Classification→ACL map: `directory/acl_classifications.json`.\n"]
tot=0
for folder,(tool,fmt) in DESC.items():
    files=sorted(p for p in (DATA/folder).rglob("*") if p.is_file())
    md.append(f"\n## {folder}/ — {tool}\n{fmt}\n\n| File | Records | Format |\n|---|---|---|")
    for p in files:
        n=count(p); tot+=n
        md.append(f"| `{p.relative_to(DATA)}` | {n} | {p.suffix[1:] or 'text'} |")
    md.append(f"\n**Questions served:** {', '.join(sorted(serve[folder])) or '—'}\n")
md.append(f"\n**Total records/documents:** {tot}\n")
(ROOT/"DATA_CATALOG.md").write_text("\n".join(md)); print("catalog ok", tot)
