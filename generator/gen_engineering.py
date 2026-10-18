"""engineering/ (GitHub Enterprise-like repos, CODEOWNERS, commits, PRs/reviews; Jira BRM project incl. RAID)."""
from common import *
from datetime import date, timedelta
CLS="INTERNAL-ACCOUNT-BRM"
REPOS={"brightmart/loyalty-service":"Points accrual/redemption service (Java 17, Kafka, Azure Functions bindings)",
       "brightmart/loyalty-ingest-pipeline":"Nightly + streaming ingest of POS/e-com transactions (Spark, ADF)",
       "brightmart/member-portal-web":"Member portal (React)","brightmart/infra-brightmart":"AKS, Event Hubs, Terraform"}

def d_range(a,b,r):
    a=date.fromisoformat(a); b=date.fromisoformat(b); return (a+timedelta(days=r.randint(0,(b-a).days))).isoformat()

def run():
    r=rng("engineering")
    write_json("engineering/repositories.json",[{**meta("GitHub Enterprise",f"REPO-{n.split('/')[1]}",ts("2026-03-02"),CLS),"full_name":n,"description":d,
        "default_branch":"main","visibility":"internal","codeowners_file":f"codeowners/{n.split('/')[1]}.CODEOWNERS"} for n,d in REPOS.items()])
    (DATA/"engineering/codeowners").mkdir(parents=True,exist_ok=True)
    co={"loyalty-service":"# CODEOWNERS - brightmart/loyalty-service\n# Updated 2026-06-15 (PR #212): @ananya-krishnan -> @mohit-saxena (Acting Architect, STF-2026-0389)\n*                       @mohit-saxena\n/docs/adr/              @mohit-saxena\n/src/main/java/**/accrual/  @mohit-saxena\n",
        "loyalty-ingest-pipeline":"# CODEOWNERS - brightmart/loyalty-ingest-pipeline\n*            @mohit-saxena @imran-sheikh\n",
        "member-portal-web":"# CODEOWNERS - brightmart/member-portal-web\n*            @mohit-saxena\n",
        "infra-brightmart":"# CODEOWNERS - brightmart/infra-brightmart\n*            @arvind-pandey @mohit-saxena\n"}
    for k,v in co.items(): (DATA/f"engineering/codeowners/{k}.CODEOWNERS").write_text(v)
    write_json("engineering/codeowners_history.json",[
        {**meta("GitHub Enterprise","CO-loyalty-service-1",ts("2026-03-09"),CLS),"repo":"brightmart/loyalty-service","pattern":"*","owners":["@ananya-krishnan"],"valid_from":"2026-03-09","valid_to":"2026-06-14","commit":"PR #3"},
        {**meta("GitHub Enterprise","CO-loyalty-service-2",ts("2026-06-15"),CLS),"repo":"brightmart/loyalty-service","pattern":"*","owners":["@mohit-saxena"],"valid_from":"2026-06-15","valid_to":None,"commit":"PR #212"}])
    # weights per repo: (person, commit weight, review weight)
    W={"brightmart/loyalty-service":[("NW1106",30,80),("NW1104",10,18),("NW1105",10,5),("NW1110",14,3),("NW1114",12,3),("NW1107",8,3),("NW1116",4,0)],
       "brightmart/loyalty-ingest-pipeline":[("NW1109",40,20),("NW1106",25,45),("NW1113",10,5),("NW1105",5,20),("NW1116",8,0)],
       "brightmart/member-portal-web":[("NW1108",50,20),("NW1110",20,30),("NW1105",5,40),("NW1111",5,10)],
       "brightmart/infra-brightmart":[("NW1112",60,10),("NW1105",10,50),("NW1106",5,30)]}
    events=[]; prs=[]; n_pr=0
    for repo,wl in W.items():
        npr={"brightmart/loyalty-service":70,"brightmart/loyalty-ingest-pipeline":30,"brightmart/member-portal-web":25,"brightmart/infra-brightmart":15}[repo]
        for _ in range(npr):
            n_pr+=1; num=n_pr
            author=r.choices([p for p,_,_ in wl],[c for _,c,_ in wl])[0]
            lo="2026-03-02"; hi="2026-09-25"
            if author=="NW1104": hi="2026-06-12"
            d=d_range(lo,hi,r)
            revs=[p for p,_,rw in wl if p!=author and rw>0 and not (p=="NW1104" and d>"2026-06-12")]
            ww=[rw for p,_,rw in wl if p!=author and rw>0 and not (p=="NW1104" and d>"2026-06-12")]
            reviewer=r.choices(revs,ww)[0]
            pid=f"PR-{repo.split('/')[1]}-{num}"
            area=r.choice(["accrual","redemption","kafka-consumer","dlq","schema-registry","functions-binding","tests","config"]) if "loyalty-service" in repo else r.choice(["etl","job","ui","infra","tests"])
            pr={**meta("GitHub Enterprise",pid,ts(d,"11:00:00"),CLS,updated_at=ts(d,"17:00:00")),"repo":repo,"number":num,
                "title":f"{area}: {r.choice(['fix','feat','refactor','chore'])} {r.choice(['idempotent offsets','retry backoff','partition rebalance','points rounding','null member ids','batch size','alerting','flaky test'])}",
                "author":handle(author),"author_id":author,"state":"merged","merged_at":ts(d,"17:00:00"),
                "reviews":[{"reviewer":handle(reviewer),"reviewer_id":reviewer,"state":"APPROVED","submitted_at":ts(d,"16:00:00"),"comments":r.randint(0,9)}],
                "jira":f"BRM-{r.randint(100,420)}"}
            if r.random()<0.25:
                rv2=r.choices(revs,ww)[0]
                if rv2!=reviewer: pr["reviews"].insert(0,{"reviewer":handle(rv2),"reviewer_id":rv2,"state":"CHANGES_REQUESTED","submitted_at":ts(d,"13:00:00"),"comments":r.randint(1,6)})
            prs.append(pr)
            events.append({"type":"pull_request.merged","id":pid,"actor":author,"at":pr["merged_at"],"repo":repo})
            for rv in pr["reviews"]: events.append({"type":"pull_request_review.submitted","id":pid,"actor":rv["reviewer_id"],"state":rv["state"],"at":rv["submitted_at"],"repo":repo})
    # fixed story PRs
    prs.append({**meta("GitHub Enterprise","PR-loyalty-service-212",ts("2026-06-15","10:00:00"),CLS),"repo":"brightmart/loyalty-service","number":212,
        "title":"chore: CODEOWNERS -> @mohit-saxena (acting architect)","author":"mohit-saxena","author_id":"NW1105","state":"merged","merged_at":ts("2026-06-15","12:00:00"),
        "reviews":[{"reviewer":"deepa-narayan","reviewer_id":"NW1106","state":"APPROVED","submitted_at":ts("2026-06-15","11:30:00"),"comments":0}],"jira":"BRM-301"})
    prs.append({**meta("GitHub Enterprise","PR-loyalty-service-198",ts("2026-06-11","10:00:00"),CLS),"repo":"brightmart/loyalty-service","number":198,
        "title":"docs: loyalty-service runbook (on-call, DLQ replay, offset reset, partition rebalance)","author":"deepa-narayan","author_id":"NW1106","state":"merged","merged_at":ts("2026-06-12","18:00:00"),
        "reviews":[{"reviewer":"ananya-krishnan","reviewer_id":"NW1104","state":"APPROVED","submitted_at":ts("2026-06-12","17:00:00"),"comments":4}],"jira":"BRM-298",
        "files":["docs/runbook.md"]})
    commits=[]
    for pr in prs:
        for k in range(r.randint(1,4)):
            sha="%040x"%r.getrandbits(160)
            commits.append({**meta("GitHub Enterprise",f"C-{sha[:10]}",pr["merged_at"],CLS),"sha":sha,"repo":pr["repo"],"author_id":pr["author_id"],
                            "author":{"name":NAME[pr["author_id"]],"email":login(pr["author_id"])},"committed_at":pr["created_at"],"pr":pr["id"],"message":pr["title"]})
    write_json("engineering/pull_requests.json",prs)
    write_json("engineering/commits.json",commits)
    write_json("engineering/file_history_runbook.json",[{**meta("GitHub Enterprise","FILE-loyalty-service-docs-runbook",ts("2026-06-12","18:00:00"),CLS,updated_at=ts("2026-09-10")),
        "repo":"brightmart/loyalty-service","path":"docs/runbook.md","created_by":"NW1106","last_modified_by":"NW1106",
        "blame_share":{"NW1106":0.91,"NW1105":0.06,"NW1104":0.03},"front_matter_owner":"@deepa-narayan"}])
    # Jira
    J=[]
    raid=[("BRM-RAID-R01","Risk","Key-person dependency on loyalty-service after architect move (Ananya -> HarbourBank)","NW1105","Open","High",["MOM-2026-024","STF-2026-0389"],"2026-05-22"),
          ("BRM-RAID-R02","Risk","Peak trading freeze 15 Nov-31 Dec limits release window for M3","NW1102","Open","High",["SOW-BRM-01"],"2026-03-05"),
          ("BRM-RAID-R03","Risk","Margin below floor if onsite days exceed plan","NW1101","Mitigating","Medium",["APR-2026-0331"],"2026-04-02"),
          ("BRM-RAID-I01","Issue","Late-night pipeline failures with no on-call laptop pool","NW1109","Closed","Medium",["PIR-2026-014"],"2026-07-15"),
          ("BRM-RAID-I02","Issue","CAPA-1 Citrix device compliance overdue (80%)","NW1103","Open","High",["ITT-2026-2210","AUD-2026-01"],"2026-09-16"),
          ("BRM-RAID-I03","Issue","Brightmart escalation: remediation plan & audit right §16","NW1103","Open","Critical",["ESC-2026-005","DEC-2026-036"],"2026-08-06"),
          ("BRM-RAID-A01","Assumption","Brightmart Citrix change freeze allows gateway policy change by 2026-10-09","NW1701","Open","Medium",["ITT-2026-2210"],"2026-09-15"),
          ("BRM-RAID-D01","Dependency","Brightmart Citrix team to apply device assurance on 2 remaining gateway nodes","NW1103","Open","High",["ITT-2026-2210"],"2026-09-15"),
          ("BRM-RAID-D02","Dependency","Permanent architect nomination needs Brightmart approval (MSA §9)","NW1101","Open","Medium",["APR-2026-0610"],"2026-06-10")]
    for k,t,s,o,st,pr_,links,d in raid:
        J.append({**meta("Jira Cloud",k,ts(d),CLS,updated_at=ts("2026-09-20")),"key":k,"project":"BRM","issuetype":"RAID","raid_type":t,"summary":s,
                  "assignee":o,"status":st,"priority":pr_,"links":links,"created":d})
    J.append({**meta("Jira Cloud","BRM-298",ts("2026-06-08"),CLS),"key":"BRM-298","issuetype":"Task","summary":"Write loyalty-service runbook before architect handover","assignee":"NW1106","reporter":"NW1104","status":"Done","links":["MOM-2026-024"]})
    J.append({**meta("Jira Cloud","BRM-301",ts("2026-06-15"),CLS),"key":"BRM-301","issuetype":"Task","summary":"Transfer CODEOWNERS to acting architect","assignee":"NW1105","status":"Done"})
    J.append({**meta("Jira Cloud","BRM-377",ts("2026-07-14","22:30:00"),CLS),"key":"BRM-377","issuetype":"Incident","summary":"ldp-accrual-nightly failed (Spark executor OOM) - points not accrued","assignee":"NW1109","status":"Done","resolved":ts("2026-07-15","01:30:00")})
    team=["NW1106","NW1107","NW1108","NW1109","NW1110","NW1112","NW1113","NW1114","NW1116","NW1111"]
    tw=[30,6,10,12,10,8,5,7,4,8]
    for i in range(120):
        k=f"BRM-{100+i}"
        if k in ("BRM-298","BRM-301","BRM-377"): continue
        a=r.choices(team,tw)[0]; d=d_range("2026-03-02","2026-09-25",r)
        typ=r.choice(["Story","Bug","Task","Bug"])
        J.append({**meta("Jira Cloud",k,ts(d),CLS),"key":k,"issuetype":typ,"summary":f"{typ}: loyalty {r.choice(['accrual','redemption','ingest','portal','alerting','DLQ'])} item","assignee":a,
                  "component":r.choice(["loyalty-service","ingest","portal","infra"]),"status":r.choice(["Done"]*4+["In Progress","To Do"]),"created":d})
        events.append({"type":"jira.issue.resolved","id":k,"actor":a,"at":ts(d,"18:00:00"),"repo":None})
    write_json("engineering/jira_issues.json",J)
    events.sort(key=lambda e:e["at"])
    write_jsonl("engineering/activity_events.jsonl",[{**meta("GitHub Enterprise / Jira Cloud",f"ACT-{i+1:05d}",e["at"],CLS),**{k:v for k,v in e.items() if k!='id'},"object_id":e["id"]} for i,e in enumerate(events)])
    # summary
    from collections import Counter
    c=Counter((e["actor"],e["type"]) for e in events if (e.get("repo") or "")=="brightmart/loyalty-service")
    return {"prs":len(prs),"commits":len(commits),"jira":len(J),"events":len(events),
            "ls_reviews":{NAME[p]:c[(p,"pull_request_review.submitted")] for p in ["NW1106","NW1105","NW1104","NW1801"]}}
