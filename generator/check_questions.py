"""Validate every evidence reference in demo_questions.json resolves to an existing record/clause; also basic envelope checks."""
import json, re, csv, sys
from pathlib import Path
from common import DATA, ROOT
idx={}   # id -> file
hris=set()
def add(i,f):
    if isinstance(i,str): idx.setdefault(i,f)
def walk(o,f):
    if isinstance(o,dict):
        if "id" in o: add(o["id"],f)
        if "key" in o: add(o["key"],f)
        for v in o.values(): walk(v,f)
    elif isinstance(o,list):
        for v in o: walk(v,f)
missing_env=[]
REQ={"id","source_system","created_at","classification","acl"}
for p in DATA.rglob("*"):
    rel=str(p.relative_to(DATA))
    if p.suffix==".json":
        o=json.loads(p.read_text()); walk(o,rel)
        if rel=="hris/job_events.json":
            for e in o: hris.add(f"HRIS:{e['worker_id']}@{e['effective_date']}")
        recs = o.get("Resources",o) if isinstance(o,dict) else o
        if isinstance(recs,list):
            for r in recs[:3000]:
                env = r.get("x_northwind",r) if isinstance(r,dict) else {}
                if isinstance(env,dict) and not REQ<=set(env): missing_env.append(rel); break
    elif p.suffix==".jsonl":
        for l in p.read_text().splitlines(): walk(json.loads(l),rel)
    elif p.suffix==".csv":
        for r in csv.DictReader(p.open()):
            if r.get("id"): add(r["id"],rel)
    elif p.suffix==".md":
        t=p.read_text(); add(p.stem,rel)
        m=re.search(r"^---\n(.*?)\n---",t,re.S)
        if m:
            fm=dict(re.findall(r"^(\w+):\s*(.*)$",m.group(1),re.M))
            for k in ("id","doc_id"): add(fm.get(k,"").strip(),rel)
            if not REQ<=set(fm): missing_env.append(rel)
for gid in json.loads((DATA/"directory/scim_groups.json").read_text())["Resources"]: add(gid["id"],"directory/scim_groups.json")
def clause_ok(doc,c):
    f=idx.get(doc)
    if not f: return False
    t=(DATA/f).read_text()
    return re.search(rf"(§{re.escape(c)}\b|\*\*{re.escape(c)}(\*\*|[ .])|\*\*§{re.escape(c)}\b|Annex {re.escape(c)})",t) is not None
Q=json.loads((ROOT/"demo_questions.json").read_text())
bad=0; lines=[]
for q in Q:
    res=[]
    for e in q["evidence"]:
        if e.startswith("HRIS:"): ok=e in hris; where="hris/job_events.json"
        elif "#" in e: d,c=e.split("#",1); ok=clause_ok(d,c); where=idx.get(d,"?")
        else: ok=e in idx; where=idx.get(e,"?")
        res.append(f"{'OK ' if ok else 'MISSING'} {e} -> {where}"); bad+= (not ok)
    lines.append(f"{q['id']}: "+"; ".join(res))
out="\n".join(lines)+f"\n\nTOTAL questions={len(Q)} evidence_refs={sum(len(q['evidence']) for q in Q)} missing={bad}\nrecords indexed={len(idx)}\nfiles missing envelope fields={sorted(set(missing_env))}\n"
(ROOT/"validation_report.txt").write_text(out); print(out)
if __name__ == "__main__": sys.exit(1 if bad else 0)
