"""Shared constants, cast, ACL helpers for the Northwind synthetic dataset.
Deterministic: all randomness flows from NORTHWIND_SEED (default 2026)."""
import json, os, random, csv
from pathlib import Path

SEED = int(os.environ.get("NORTHWIND_SEED", "2026"))
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEMO_DATE = "2026-09-28"
TZ = "+05:30"

def rng(name):
    """Independent seeded RNG per module so modules don't perturb each other."""
    return random.Random(f"{SEED}:{name}")

def ts(d, t="10:00:00", tz=TZ):
    return f"{d}T{t}{tz}"

# ---------- ACL groups (directory group displayName -> id) ----------
GROUPS = {
    "grp-employee": "All Employees",
    "grp-pm": "Project Managers",
    "grp-dm": "Delivery Managers",
    "grp-unit-head": "Unit Heads",
    "grp-hr": "HR",
    "grp-hr-con": "HR - Consumer scope",
    "grp-finance": "Finance",
    "grp-pricing-desk": "Pricing Desk",
    "grp-legal": "Legal & Compliance",
    "grp-security": "IT Security & GRC",
    "grp-auditor-int": "Internal Audit",
    "grp-auditor-ext": "External Auditors (Ashgrove, time-boxed)",
    "grp-exec": "Executive Leadership",
    "grp-rmg": "Resource Management",
    "grp-mgr": "Managers G5+",
    "grp-acc-brm": "Account Team - Brightmart UK",
    "grp-acc-brm-dm": "Account Leadership - Brightmart (DM/Client Partner)",
    "grp-acc-cin": "Account Team - Coastline Insurance",
    "grp-acc-hbk": "Account Team - HarbourBank",
    "grp-acc-zen": "Account Team - Zentro Retail",
    "grp-acc-med": "Account Team - Medisure Health",
    "grp-acc-ver": "Account Team - Veridian Pharma",
}

# classification label -> default ACL (groups and/or users)
CLASS_ACL = {
    "PUBLIC-INT": ["grp-employee"],
    "MGR": ["grp-mgr", "grp-exec"],
    "RESTRICTED-FIN": ["grp-finance", "grp-exec"],
    "RESTRICTED-FIN-DM": ["grp-finance", "grp-exec", "grp-pricing-desk", "grp-acc-brm-dm", "NW0011", "NW0013"],
    "RESTRICTED-HR": ["NW0004", "grp-hr-con"],   # HRC: CHRO + scoped HRBP
    "CLIENT-CONF-BRM": ["grp-acc-brm", "grp-legal", "grp-exec", "grp-auditor-ext", "grp-auditor-int"],
    "CLIENT-CONF-BRM-OPS": ["grp-acc-brm", "grp-legal", "grp-exec", "grp-hr", "grp-auditor-ext", "grp-auditor-int"],
    "CLIENT-CONF-CIN": ["grp-acc-cin", "grp-legal", "grp-exec", "grp-rmg"],
    "CLIENT-CONF-OTHER": ["grp-legal", "grp-exec"],
    "SECURITY": ["grp-security", "grp-auditor-ext", "grp-auditor-int", "grp-exec"],
    "SECURITY-BRM": ["grp-security", "grp-auditor-ext", "grp-auditor-int", "grp-exec", "grp-legal", "grp-acc-brm"],
    "INTERNAL-ACCOUNT-BRM": ["grp-acc-brm", "grp-exec", "grp-rmg", "NW0011", "NW0013"],
    "PERSONAL": [],  # owner + approvers only; filled per record
}

def meta(source_system, id_, created_at, classification, acl=None, updated_at=None, restricted_fields=None, **extra):
    m = {
        "id": id_,
        "source_system": source_system,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "classification": classification,
        "acl": acl if acl is not None else list(CLASS_ACL[classification]),
    }
    if restricted_fields:
        m["restricted_fields"] = restricted_fields
    m.update(extra)
    return m

def rf(path, classification, note=""):
    d = {"field": path, "classification": classification, "acl": CLASS_ACL[classification]}
    if note: d["existence_disclosure"] = note
    return d

def write_json(rel, obj):
    p = DATA / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    return p

def write_jsonl(rel, rows):
    p = DATA / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p

def write_csv(rel, rows, fields):
    p = DATA / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    return p

# ---------- Named cast (state as of DEMO_DATE) ----------
# id, name, grade, title, unit, loc, mgr, joined, skills
CAST = [
 ("NW0001","Rohan Mehra","G10","Chief Executive Officer","EXEC","BLR",None,"2009-06-01","strategy;BFSI sales"),
 ("NW0002","Anjali Kapoor","G10","Chief Financial Officer","FIN","BLR","NW0001","2014-02-17","corporate finance;pricing"),
 ("NW0003","David Whitfield","G10","COO & MD, Northwind Consulting UK Ltd","EXEC","LON","NW0001","2016-09-05","delivery ops;UK market"),
 ("NW0004","Sunita Rao","G9","Chief Human Resources Officer","HR","BLR","NW0001","2015-01-12","HR policy;org design"),
 ("NW0005","Farhan Qureshi","G9","General Counsel & Chief Compliance Officer","LGL","BLR","NW0001","2017-07-03","contracts;regulatory"),
 ("NW0006","Meera Iyer","G9","Chief Information Security Officer","ITS","BLR","NW0003","2018-03-19","ISO 27001;SOC 2;IAM"),
 ("NW0007","Arjun Nair","G9","CTO & Head, Cloud & Data","CDP","BLR","NW0001","2012-11-26","cloud architecture;data platforms"),
 ("NW0010","Kavita Menon","G8","Head, Banking & Financial Services","BFS","PNQ","NW0003","2013-05-06","core banking;delivery"),
 ("NW0011","Rahul Bhatia","G8","Deputy Head, Consumer (India)","CON","BLR","NW0013","2014-08-18","retail;POS;e-com"),
 ("NW0012","Lakshmi Pillai","G8","Head, Healthcare","HLC","BLR","NW0003","2015-10-05","HL7/FHIR;healthcare IT"),
 ("NW0013","Eleanor Hughes","G8","Head, Consumer","CON","LON","NW0003","2019-04-01","UK retail;client partnering"),
 ("NW0014","Sanjay Kulkarni","G8","Head, Resource Management","RMG","PNQ","NW0003","2016-01-11","workforce planning"),
 ("NW0015","Neha Joshi","G7","Financial Controller","FIN","BLR","NW0002","2018-06-04","controllership;revenue recognition"),
 ("NW0016","Ramesh Choudhary","G7","Practice Lead, Cloud & Data","CDP","BLR","NW0007","2017-02-20","Azure;data engineering"),
 ("NW0017","Harish Gupta","G7","Head, Internal Audit","IA","BLR","NW0002","2019-09-09","IT audit;ISO lead auditor"),
 ("NW0201","Aditi Verma","G4","Lead Financial Analyst (Delivery Finance)","FIN","BLR","NW0015","2021-07-12","margin analysis;rate cards"),
 ("NW0202","Manoj Pillai","G5","Finance Manager, Pricing Desk","FIN","BLR","NW0015","2018-11-05","deal pricing"),
 ("NW0203","Rachel Moore","G5","Finance Manager, UK Ltd","FIN","LON","NW0015","2020-03-02","UK VAT;billing"),
 ("NW0301","Pooja Reddy","G6","Senior HR Business Partner, Consumer","HR","BLR","NW0004","2017-04-24","employee relations;policy"),
 ("NW0302","Thomas Green","G5","HR Manager, UK","HR","LON","NW0004","2021-01-18","UK employment law"),
 ("NW0303","Swati Bhat","G5","HR Policy Manager","HR","BLR","NW0004","2019-06-10","policy drafting"),
 ("NW0401","Ishaan Malhotra","G6","Senior Counsel, Commercial Contracts","LGL","BLR","NW0005","2020-08-17","MSAs;SOWs"),
 ("NW0402","Grace Okafor","G7","Data Protection Officer (Group)","LGL","LON","NW0005","2021-05-04","GDPR;UK DPA 2018"),
 ("NW0501","Karthik Subramanian","G6","IT Security Manager","ITS","BLR","NW0006","2016-12-05","endpoint security;MDM"),
 ("NW0502","Divya Shetty","G5","ISO 27001 Compliance Lead (ISMS owner)","ITS","BLR","NW0006","2019-02-11","ISMS;risk registers"),
 ("NW0503","Abhishek Rana","G3","SOC Analyst","ITS","BLR","NW0501","2023-03-06","SIEM;DLP"),
 ("NW0801","Bhavna Thakur","G5","RM Manager, Consumer & BFS","RMG","PNQ","NW0014","2018-07-16","staffing"),
 ("NW0802","Yusuf Ansari","G3","Staffing Analyst","RMG","PNQ","NW0801","2022-09-12","skill matching"),
 ("NW1101","Siddharth Chauhan","G6","Delivery Manager, Brightmart","CON","BLR","NW0013","2015-03-09","retail delivery;P&L"),
 ("NW1102","Ritu Agarwal","G5","Project Manager, Medisure Health","HLC","BLR","NW1501","2018-01-22","agile PM"),
 ("NW1103","Nikhil Bose","G5","Project Manager, Brightmart","CON","BLR","NW1101","2017-10-30","PM;incident mgmt"),
 ("NW1104","Ananya Krishnan","G6","Solution Architect, HarbourBank","CDP","BLR","NW0016","2016-06-27","Azure;event-driven architecture;loyalty platforms"),
 ("NW1105","Mohit Saxena","G4","Tech Lead & Acting Architect, Brightmart","CON","BLR","NW1101","2020-02-03","Java;Kafka"),
 ("NW1106","Deepa Narayan","G3","Senior Engineer","CON","BLR","NW1105","2021-08-09","Kafka;Azure Functions;data pipelines"),
 ("NW1107","Varun Shah","G3","Senior Engineer","CON","BLR","NW1105","2020-11-16","Guidewire;Java"),
 ("NW1108","Sneha Patil","G2","Software Engineer","CON","BLR","NW1105","2023-07-03","React"),
 ("NW1109","Imran Sheikh","G3","Senior Data Engineer","CON","BLR","NW1105","2021-02-15","Spark;SQL"),
 ("NW1110","Kiran Das","G2","Software Engineer","CON","BLR","NW1105","2023-01-09","Node.js"),
 ("NW1111","Megha Sinha","G4","QA Lead","CON","BLR","NW1101","2019-05-20","test automation"),
 ("NW1112","Arvind Pandey","G3","DevOps Engineer","CON","BLR","NW1105","2020-09-07","AKS;Terraform"),
 ("NW1113","Tanvi Mishra","G2","Data Analyst","CON","BLR","NW1109","2024-02-12","Power BI"),
 ("NW1114","Rajesh Yadav","G2","Software Engineer","CON","PNQ","NW1105","2022-06-20","Java"),
 ("NW1115","Fatima Khan","G3","Business Analyst","CON","LON","NW1101","2021-10-04","retail BA;onsite coordination"),
 ("NW1116","Aakash Jain","G1","Associate Engineer","CON","BLR","NW1105","2025-08-04","Python"),
 ("NW1117","Shreya Ghosh","G2","Security/Compliance Champion (part-time)","CON","BLR","NW1101","2023-04-17","DLP;access reviews"),
 ("NW1118","James Carter","G7","Client Partner, Brightmart","CON","LON","NW0013","2018-02-12","UK retail relationships"),
 ("NW1201","Prakash Hegde","G5","Project Manager, Coastline Insurance","BFS","PNQ","NW1202","2016-04-11","Guidewire PM"),
 ("NW1202","Sameer Desai","G6","Delivery Manager, Coastline Insurance","BFS","PNQ","NW0010","2014-09-01","insurance delivery"),
 ("NW1301","Oliver Hayes","G6","Delivery Manager, HarbourBank","BFS","LON","NW0010","2019-11-11","payments;UK banking"),
 ("NW1302","Lavanya Raghavan","G5","Project Manager, HarbourBank","BFS","PNQ","NW1301","2018-08-06","PM"),
 ("NW1401","Gaurav Kohli","G6","Delivery Manager, Zentro Retail","CON","BLR","NW0011","2016-05-16","omnichannel"),
 ("NW1501","Shalini Mukherjee","G6","Delivery Manager, Medisure Health","HLC","BLR","NW0012","2015-12-07","hospital systems"),
 ("NW1601","Stefan Bauer","G6","Delivery Manager, Veridian Pharma","HLC","LON","NW0012","2020-06-15","GxP;pharma"),
 ("NW1701","Rakesh Venkat","G4","Tech Lead, Internal Platform (ServiceDesk/MDM)","ITS","BLR","NW0501","2019-03-25","Intune;ServiceNow"),
 ("NW1801","Kunal Arora","G3","Senior Engineer","BENCH","BLR","NW0801","2020-01-13","Kafka"),
]
EXTERNAL = [
 ("EXT-BM-01","Sarah Mitchell","Brightmart UK","CIO, executive sponsor"),
 ("EXT-BM-02","Mark Lewis","Brightmart UK","Head of Vendor Management"),
 ("EXT-BM-03","Hannah Price","Brightmart UK","Data Protection Officer"),
 ("EXT-CI-01","Daniel Reyes","Coastline Insurance","Programme Director"),
 ("EXT-AA-01","Claire Bennett","Ashgrove Assurance","ISO 27001 Lead Auditor"),
 ("EXT-AA-02","Vivek Sharma","Ashgrove Assurance","Auditor"),
]
CAST_BY_ID = {c[0]: c for c in CAST}
NAME = {c[0]: c[1] for c in CAST} | {e[0]: e[1] for e in EXTERNAL}
UNIT_NAMES = {"EXEC":"Executive Office","BFS":"Banking & Financial Services","RTL":"Retail","UKD":"UK Delivery",
 "CON":"Consumer","HLC":"Healthcare","CDP":"Cloud & Data Practice","RMG":"Resource Management","HR":"Human Resources",
 "FIN":"Finance","LGL":"Legal & Compliance","ITS":"IT / Security","IA":"Internal Audit","BENCH":"Bench (RMG pool)"}
BRM_TEAM = [f"NW{n}" for n in range(1106,1118)]
def login(pid):
    n = NAME[pid].lower().replace(" ", ".")
    return f"{n}@northwind-consulting.com"
def handle(pid):
    return NAME[pid].lower().replace(" ", "-")

# Spec §2.3: allocations STF-2026-0141..0152 for NW1106..NW1117, with Varun (NW1107) = STF-2026-0146
_order = ["NW1106","NW1111","NW1108","NW1109","NW1110","NW1107","NW1112","NW1113","NW1114","NW1115","NW1116","NW1117"]
STF_ID = {p: f"STF-2026-{141+i:04d}" for i,p in enumerate(_order)}
