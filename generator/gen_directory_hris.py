"""directory/ (SCIM 2.0 Users+Groups, Okta/Entra-like) and hris/ (Workday-like job events, skills, HR cases)."""
from common import *
from people import named, background, everyone

SCIM_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_ENT = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
NW_EXT = "urn:northwind:params:scim:schemas:extension:hr:1.0:User"
LOCS = {"BLR":("Bangalore","IN","Northwind Consulting Pvt Ltd"),"PNQ":("Pune","IN","Northwind Consulting Pvt Ltd"),
        "LON":("London","GB","Northwind Consulting UK Ltd")}

# who belongs to which access groups (named)
ROLE = {
 "grp-exec":["NW0001","NW0002","NW0003"],
 "grp-unit-head":["NW0010","NW0011","NW0012","NW0013"],
 "grp-pm":["NW1102","NW1103","NW1201","NW1302"],
 "grp-dm":["NW1101","NW1202","NW1301","NW1401","NW1501","NW1601"],
 "grp-hr":["NW0004","NW0301","NW0302","NW0303"],
 "grp-hr-con":["NW0301"],
 "grp-finance":["NW0002","NW0015","NW0201","NW0202","NW0203"],
 "grp-pricing-desk":["NW0202"],
 "grp-legal":["NW0005","NW0401","NW0402"],
 "grp-security":["NW0006","NW0501","NW0502","NW0503","NW1701"],
 "grp-auditor-int":["NW0017"],
 "grp-auditor-ext":["EXT-AA-01","EXT-AA-02"],
 "grp-rmg":["NW0014","NW0801","NW0802"],
 "grp-acc-brm":["NW1101","NW1103","NW1105","NW1118"]+BRM_TEAM,
 "grp-acc-brm-dm":["NW1101","NW1118"],
 "grp-acc-cin":["NW1201","NW1202"],
 "grp-acc-hbk":["NW1301","NW1302","NW1104"],
 "grp-acc-zen":["NW1401"],
 "grp-acc-med":["NW1501","NW1102"],
 "grp-acc-ver":["NW1601"],
}

def user(p, external=None):
    if external:
        eid, nm, org, title = external
        first, last = nm.split(" ",1)
        return {"schemas":[SCIM_USER, SCIM_ENT],
          "id": eid, "externalId": eid, "userName": f"{first.lower()}.{last.lower()}@ashgrove-assurance.example",
          "name":{"givenName":first,"familyName":last,"formatted":nm}, "displayName":nm, "title":title,
          "userType":"Guest","active":True,
          "emails":[{"value":f"{first.lower()}.{last.lower()}@ashgrove-assurance.example","type":"work","primary":True}],
          SCIM_ENT:{"organization":org},
          "meta":{"resourceType":"User","created":ts("2026-07-20"),"lastModified":ts("2026-07-20"),
                  "location":f"https://directory.northwind.example/scim/v2/Users/{eid}"},
          "x_northwind": meta("Okta", eid, ts("2026-07-20"), "MGR",
                  acl=["grp-security","grp-exec","grp-auditor-ext"],
                  access_window={"valid_from":"2026-07-28","valid_to":"2026-10-30"})}
    first, last = p["name"].split(" ",1)
    city, cc, org = LOCS[p["loc"]]
    em = p["name"].lower().replace(" ",".") + "@northwind-consulting.com"
    status = "active"
    u = {"schemas":[SCIM_USER, SCIM_ENT, NW_EXT],
      "id": p["id"], "externalId": p["id"], "userName": em,
      "name":{"givenName":first,"familyName":last,"formatted":p["name"]},
      "displayName":p["name"], "title":p["title"], "userType":"Contractor" if p.get("contractor") else "Employee",
      "active":True, "preferredLanguage":"en-GB" if p["loc"]=="LON" else "en-IN",
      "emails":[{"value":em,"type":"work","primary":True}],
      "addresses":[{"type":"work","locality":city,"country":cc,"primary":True}],
      SCIM_ENT:{"employeeNumber":p["id"],"organization":org,"division":UNIT_NAMES[p["unit"]],
                "department":p["unit"],"costCenter":f"CC-{p['unit']}-{p['loc']}",
                **({"manager":{"value":p["mgr"],"$ref":f"../Users/{p['mgr']}","displayName":NAME.get(p["mgr"]) or BGNAME.get(p["mgr"],"")}} if p["mgr"] else {})},
      NW_EXT:{"grade":p["grade"],"location_code":p["loc"],"hire_date":p["joined"]},
      "meta":{"resourceType":"User","created":ts(p["joined"]),"lastModified":ts("2026-09-01"),
              "location":f"https://directory.northwind.example/scim/v2/Users/{p['id']}"},
      "x_northwind": meta("Okta", p["id"], ts(p["joined"]), "PUBLIC-INT", updated_at=ts("2026-09-01")),
    }
    return u

BGNAME = {}

def run():
    bg = background()
    for p in bg: BGNAME[p["id"]] = p["name"]
    allp = named() + bg
    users = [user(p) for p in allp] + [user(None, e) for e in EXTERNAL if e[0].startswith("EXT-AA")]
    write_json("directory/scim_users.json", {"schemas":["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
               "totalResults":len(users),"itemsPerPage":len(users),"startIndex":1,"Resources":users})
    # groups
    members = {k: list(v) for k,v in ROLE.items()}
    members["grp-employee"] = [p["id"] for p in allp]
    gnum = lambda g:int(g[1:])
    members["grp-mgr"] = [p["id"] for p in allp if gnum(p["grade"])>=5]
    r = rng("groups")
    for p in bg:
        u = p["unit"]
        if u=="HR": members["grp-hr"].append(p["id"])
        if u=="FIN": members["grp-finance"].append(p["id"])
        if u=="LGL": members["grp-legal"].append(p["id"])
        if u=="ITS" and p["title"].startswith("Security"): members["grp-security"].append(p["id"])
        if u=="RMG": members["grp-rmg"].append(p["id"])
        if u=="IA": members["grp-auditor-int"].append(p["id"])
        if u in ("BFS","CON","HLC","CDP") and p["title"]=="Project Manager": members["grp-pm"].append(p["id"])
        if u in ("BFS","CON","HLC","CDP") and p["title"]=="Delivery Manager": members["grp-dm"].append(p["id"])
    groups = []
    for gid, disp in GROUPS.items():
        g = {"schemas":["urn:ietf:params:scim:schemas:core:2.0:Group"],"id":gid,"displayName":disp,
             "members":[{"value":m,"$ref":f"../Users/{m}","display":NAME.get(m) or BGNAME.get(m)} for m in members.get(gid,[])],
             "meta":{"resourceType":"Group","created":ts("2025-01-01"),"lastModified":ts("2026-09-01")},
             "x_northwind": meta("Okta", gid, ts("2025-01-01"), "MGR", acl=["grp-security","grp-exec","grp-auditor-ext","grp-auditor-int"], updated_at=ts("2026-09-01"))}
        if gid=="grp-auditor-ext":
            g["x_northwind"]["access_window"]={"valid_from":"2026-07-28","valid_to":"2026-10-30","basis":"Ashgrove engagement letter AA-NW-2026-SA1"}
            g["x_northwind"]["pseudonymise_persons_in_hr_context"]=True
        if gid=="grp-acc-brm":
            g["x_northwind"]["membership_history"]=[
              {"member":"NW1102","from":"2026-02-13","to":"2026-08-24"},{"member":"NW1104","from":"2026-01-20","to":"2026-06-15"},
              {"member":"NW1103","from":"2026-08-24","to":None}]
            members_hist = [{"value":"NW1102"},{"value":"NW1104"}]
        groups.append(g)
    write_json("directory/scim_groups.json", {"schemas":["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
               "totalResults":len(groups),"Resources":groups})
    write_json("directory/acl_classifications.json", {"x_northwind":meta("Northwind IAM","ACL-MAP",ts("2026-01-01"),"PUBLIC-INT"),
       "classifications":CLASS_ACL, "groups":GROUPS,
       "rules":["Visibility is evaluated at query time per node/edge; a path with any invisible node/edge is pruned.",
                "restricted_fields[] on a record override the record ACL for that field only.",
                "existence_disclosure on a restricted field states what may be said to a reader who cannot see the value.",
                "grp-auditor-ext is valid 2026-07-28..2026-10-30 only and sees employee IDs, not names, in HR-adjacent context.",
                "Unit-head visibility follows unit membership as of the query date (ORG-2026-05)."]})
    hris(allp, bg)
    return {"users":len(users),"groups":len(groups)}

def hris(allp, bg):
    r = rng("hris")
    ev = []
    n = [0]
    def e(worker, date, etype, reason, before, after, source, cls="MGR", acl=None, extra=None):
        n[0]+=1
        rec = {**meta("Workday", f"JE-{n[0]:06d}", ts(date,"09:00:00"), cls, acl=acl),
               "worker_id":worker,"worker_name":NAME.get(worker) or BGNAME.get(worker),
               "event_type":etype,"effective_date":date,"reason":reason,"before":before,"after":after,"source_ref":source}
        if extra: rec.update(extra)
        ev.append(rec)
    # hires
    for p in allp:
        u = p["unit"]
        hire_unit = {"NW0011":"RTL","NW0013":"UKD","NW1101":"RTL","NW1102":"RTL","NW1103":"BFS","NW1105":"RTL","NW1106":"RTL",
                     "NW1107":"BFS","NW1118":"UKD","NW1801":"CDP","NW1401":"RTL","NW1108":"RTL","NW1109":"RTL","NW1110":"BENCH","NW1111":"RTL",
                     "NW1112":"RTL","NW1113":"BENCH","NW1114":"BENCH","NW1115":"UKD","NW1116":"BENCH","NW1117":"RTL"}.get(p["id"])
        if not hire_unit: hire_unit = p.get("pre_reorg_unit") or u
        e(p["id"], p["joined"], "Hire", "New Hire", None, {"unit":hire_unit,"location":p["loc"] if p["id"]!="NW1107" else "PNQ"}, "Workday Hire BP")
    # named dated changes (spec §2.3)
    for pid in BRM_TEAM:
        e(pid,"2026-02-23","Project Assignment","Allocated to Brightmart UK",None,{"account":"ACC-BRM"},STF_ID[pid])
    e("NW1107","2026-02-23","Transfer","Released mid-project from Coastline (60% complete on claims-module migration)",
      {"unit":"BFS","manager":"NW1202","location":"PNQ","account":"ACC-CIN"},{"unit":"RTL","manager":"NW1105","location":"BLR","account":"ACC-BRM"},"STF-2026-0146; APR-2026-0219")
    e("NW0011","2026-05-01","Reorganization","ORG-2026-05 Consumer unit created",{"unit":"RTL","title":"Head, Retail","manager":"NW0003"},
      {"unit":"CON","title":"Deputy Head, Consumer (India)","manager":"NW0013"},"ORG-2026-05")
    e("NW0013","2026-05-01","Reorganization","ORG-2026-05 Consumer unit created",{"unit":"UKD","title":"Head, UK Delivery"},{"unit":"CON","title":"Head, Consumer"},"ORG-2026-05")
    e("NW1101","2026-05-01","Manager Change","ORG-2026-05",{"unit":"RTL","manager":"NW0011"},{"unit":"CON","manager":"NW0013"},"ORG-2026-05")
    e("NW1118","2026-05-01","Reorganization","ORG-2026-05 (manager unchanged)",{"unit":"UKD","manager":"NW0013"},{"unit":"CON","manager":"NW0013"},"ORG-2026-05")
    e("NW1401","2026-05-01","Reorganization","ORG-2026-05 (manager unchanged: Rahul Bhatia, now Deputy Head)",{"unit":"RTL","manager":"NW0011"},{"unit":"CON","manager":"NW0011"},"ORG-2026-05")
    for pid in ["NW1102","NW1105"]+BRM_TEAM:
        if pid=="NW1115": before="UKD"
        else: before="RTL"
        e(pid,"2026-05-01","Reorganization","ORG-2026-05 unit code change",{"unit":before},{"unit":"CON"},"ORG-2026-05")
    for p in bg:
        if p["unit"]=="CON":
            e(p["id"],"2026-05-01","Reorganization","ORG-2026-05 unit code change",{"unit":p["pre_reorg_unit"]},{"unit":"CON"},"ORG-2026-05")
    e("NW1104","2026-06-15","Project Assignment","Moved Brightmart -> HarbourBank architect (still CDP)",{"account":"ACC-BRM","title":"Solution Architect, Brightmart"},{"account":"ACC-HBK","title":"Solution Architect, HarbourBank"},"STF-2026-0388")
    e("NW1105","2026-06-15","Title Change","Acting Architect, Brightmart",{"title":"Tech Lead"},{"title":"Tech Lead & Acting Architect, Brightmart"},"STF-2026-0389")
    e("NW1102","2026-08-24","Transfer","Reassigned Brightmart PM -> Medisure Health PM",{"unit":"CON","manager":"NW1101","account":"ACC-BRM"},{"unit":"HLC","manager":"NW1501","account":"ACC-MED"},"STF-2026-0512")
    e("NW1103","2026-08-24","Transfer","HarbourBank programme -> Brightmart PM",{"unit":"BFS","manager":"NW1301","account":"ACC-HBK"},{"unit":"CON","manager":"NW1101","account":"ACC-BRM"},"STF-2026-0513")
    e("NW1109","2026-08-03","Status Change","Client-data access suspended pending investigation",{"client_data_access":"active"},{"client_data_access":"suspended"},"SEC-2026-0077",cls="RESTRICTED-HR",acl=CLASS_ACL["RESTRICTED-HR"]+["grp-security"])
    e("NW1109","2026-09-01","Status Change","Access restored after re-training",{"client_data_access":"suspended"},{"client_data_access":"active"},"ITT-2026-2301; HRC-2026-0031",cls="RESTRICTED-HR",acl=CLASS_ACL["RESTRICTED-HR"]+["grp-security"])
    # background noise: transfers, exits, promotions
    for p in r.sample(bg, 260):
        d = f"2026-{r.randint(1,9):02d}-{r.randint(1,28):02d}"
        if d < p["joined"]: continue
        k = r.choice(["Promotion","Termination","Transfer","Manager Change"])
        if k=="Termination": e(p["id"],d,"Termination",r.choice(["Resignation","End of Contract","Relocation"]),{"status":"active"},{"status":"terminated"},"Workday Termination BP")
        else: e(p["id"],d,k,k,{"grade":p["grade"]},{"grade":p["grade"]},"Workday BP")
    # resignation of Ananya? (spec: she moves account, does not exit) - exits are background only + Ritu/Nikhil moves
    ev.sort(key=lambda x:(x["effective_date"],x["id"]))
    write_json("hris/job_events.json", ev)
    write_csv("hris/job_events.csv", [{**x,"before":json.dumps(x["before"]),"after":json.dumps(x["after"])} for x in ev],
              ["id","worker_id","worker_name","event_type","effective_date","reason","before","after","source_ref","classification","source_system","created_at"])
    # workers snapshot (current position)
    write_csv("hris/workers.csv", [{"worker_id":p["id"],"name":p["name"],"grade":p["grade"],"title":p["title"],"unit":p["unit"],"location":p["loc"],
        "manager_id":p["mgr"] or "","hire_date":p["joined"],"as_of":DEMO_DATE,"classification":"PUBLIC-INT","source_system":"Workday"} for p in allp],
        ["worker_id","name","grade","title","unit","location","manager_id","hire_date","as_of","classification","source_system"])
    # skills & certs for named
    certs = {"NW1104":["Microsoft Certified: Azure Solutions Architect Expert (2024)"],"NW1106":["Confluent Certified Developer for Apache Kafka (2025)","Microsoft Certified: Azure Developer Associate (2024)"],
             "NW1105":["Oracle Java SE 17 Developer (2023)"],"NW1801":["Confluent Certified Developer for Apache Kafka (2023)"],"NW0502":["ISO/IEC 27001 Lead Implementer"],
             "NW0017":["ISO/IEC 27001 Lead Auditor","CISA"],"NW0501":["CISSP","Microsoft Intune"],"NW1701":["Microsoft Certified: Endpoint Administrator Associate"],
             "NW0402":["CIPP/E"],"NW1112":["CKA","HashiCorp Terraform Associate"],"NW1103":["PMP","ITIL 4 Foundation"],"NW1102":["PMP"],"NW1117":["CompTIA Security+"]}
    sk = []
    for p in named():
        sk.append({**meta("Workday Skills Cloud", f"SKL-{p['id']}", ts("2026-01-05"), "PUBLIC-INT"),
                   "worker_id":p["id"],"name":p["name"],"skills":[{"skill":s,"source":"self-declared"} for s in p["skills"]],
                   "certifications":certs.get(p["id"],[])})
    write_json("hris/skills_certs.json", sk)
    # HR case (restricted stub)
    hrc = [{**meta("Workday Employee Relations","HRC-2026-0031",ts("2026-08-03","15:20:00"),"RESTRICTED-HR",updated_at=ts("2026-09-01","11:00:00"),
             restricted_fields=[rf("subject_worker_id","RESTRICTED-HR","PM/DM may be told only: 'an HR matter is open/closed for this person; contact HRBP Pooja Reddy'"),
                                rf("outcome","RESTRICTED-HR"),rf("case_owner","RESTRICTED-HR")]),
            "case_type":"Policy Violation - Information Security","subject_worker_id":"NW1109","case_owner":"NW0301",
            "opened":"2026-08-03","closed":"2026-09-01","status":"Closed","outcome":"Written warning; mandatory re-training completed 2026-08-29",
            "linked":["SEC-2026-0077","INC-2026-014"],"narrative":None,
            "existence_visible_to":["grp-acc-brm-dm","NW1103"],"note":"Narrative intentionally not stored (spec §5)."}]
    for i in range(15):
        d=f"2026-{r.randint(1,9):02d}-{r.randint(1,27):02d}"
        hrc.append({**meta("Workday Employee Relations",f"HRC-2026-{i+1:04d}",ts(d),"RESTRICTED-HR",acl=["NW0004","grp-hr"]),
            "case_type":r.choice(["Attendance","Grievance","Conduct","Performance Support"]),"subject_worker_id":r.choice(bg)["id"],
            "opened":d,"status":r.choice(["Open","Closed"]),"outcome":None,"narrative":None})
    write_json("hris/hr_cases.json", hrc)
