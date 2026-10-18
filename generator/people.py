"""Procedural background workforce (~2,945) + named cast. Deterministic."""
from common import *

FIRST_IN = "Aarav Abhay Aditya Ajay Akhil Alok Amit Anand Anil Ankit Anup Arnav Ashok Ayaan Bharat Chetan Darshan Dev Dhruv Dinesh Ganesh Girish Harsh Hemant Jatin Jay Karan Keshav Krishna Lalit Madhav Mahesh Manish Mayank Mukesh Naveen Nitin Omkar Pankaj Paras Pranav Prateek Puneet Rajat Rakesh Ravi Rohit Sachin Sahil Sandeep Santosh Saurabh Shankar Shiv Shubham Sumit Sunil Suresh Tarun Tushar Uday Vikas Vikram Vinay Vishal Vivek Yash Aarti Akanksha Alka Amrita Anita Anjana Ankita Bhavya Chitra Deepika Divya Gauri Geeta Isha Jyoti Kajal Kavya Komal Lata Madhuri Mansi Meenal Neelam Nidhi Nisha Pallavi Pooja Preeti Priya Radhika Rashmi Rekha Riya Sakshi Sanjana Seema Shilpa Simran Smita Sonal Sunaina Swara Tanya Usha Vandana Varsha Vidya".split()
LAST_IN = "Acharya Agrawal Ahuja Bajaj Banerjee Bhat Chandra Chatterjee Chopra Dutta Gaikwad Garg Goel Gowda Hegde Iyengar Jadhav Jain Joshi Kamath Kapadia Karnik Khanna Kulkarni Kumar Lal Mahajan Malik Mehta Menon Mishra Mittal Nair Naik Pandit Patel Patil Prasad Rao Rathore Reddy Sahni Saini Sarkar Sethi Shah Sharma Shetty Shinde Singh Sinha Srinivasan Tiwari Trivedi Upadhyay Varma Vora Wagh Yadav Zaveri".split()
FIRST_UK = "Oliver George Harry Jack Jacob Noah Charlie Thomas Oscar William James Leo Alfie Henry Joshua Freddie Archie Ethan Isaac Mohammed Amelia Olivia Isla Emily Poppy Ava Isabella Jessica Lily Sophie Grace Sophia Mia Evie Ruby Ella Scarlett Chloe Priya Aisha".split()
LAST_UK = "Smith Jones Williams Taylor Brown Davies Evans Wilson Thomas Johnson Roberts Robinson Thompson Wright Walker White Edwards Hughes Green Hall Wood Harris Lewis Martin Jackson Clarke Clark Turner Hill Scott Cooper Morris Ward Moore King Watson Baker Harrison Morgan Patel Young Allen Mitchell Anderson Khan Ahmed Begum".split()

# target headcounts (background only) as of demo date; RTL/UKD already merged into CON
UNIT_TARGET = [("BFS",760),("CON",655),("HLC",480),("CDP",395),("BENCH",225),("RMG",22),("HR",66),("FIN",54),("LGL",21),("ITS",100),("IA",7),("EXEC",0)]
GRADE_W = [("G1",18),("G2",24),("G3",22),("G4",15),("G5",10),("G6",6),("G7",3),("G8",0.0),("G9",0.0)]
TITLES = {"G1":["Associate Engineer","Analyst Trainee"],"G2":["Software Engineer","Analyst","HR Associate"],
 "G3":["Senior Engineer","Senior Analyst","Consultant"],"G4":["Tech Lead","Lead Analyst","Assistant Manager"],
 "G5":["Project Manager","Engineering Manager","Finance Manager"],"G6":["Delivery Manager","Solution Architect","Senior Manager"],
 "G7":["Practice Lead","Client Partner","Associate Director"]}
FUNC_TITLES = {"HR":{"G2":"HR Associate","G3":"Senior HR Executive","G4":"HR Lead","G5":"HR Manager","G6":"HR Business Partner"},
 "FIN":{"G2":"Financial Analyst","G3":"Senior Financial Analyst","G4":"Lead Analyst","G5":"Finance Manager","G6":"Senior Finance Manager"},
 "LGL":{"G2":"Legal Associate","G3":"Legal Counsel","G4":"Senior Legal Counsel","G5":"Legal Manager","G6":"Senior Counsel"},
 "ITS":{"G2":"IT Support Engineer","G3":"Security Analyst","G4":"Security Lead","G5":"IT Manager","G6":"Security Manager"},
 "RMG":{"G2":"Staffing Associate","G3":"Staffing Analyst","G4":"Staffing Lead","G5":"RM Manager"},
 "IA":{"G3":"Internal Auditor","G4":"Senior Internal Auditor","G5":"Audit Manager"}}
SKILLS = {"BFS":["core banking","payments","Guidewire","COBOL"],"CON":["SAP Retail","e-com","POS","loyalty"],
 "HLC":["HL7","FHIR","Epic"],"CDP":["Azure","AWS","Databricks","Kafka"],"SHARED":["Java","Python","React","SQL","Agile"]}
UNIT_HEAD = {"BFS":"NW0010","CON":"NW1401","HLC":"NW0012","CDP":"NW0016","BENCH":"NW0801","RMG":"NW0801","HR":"NW0004",
 "FIN":"NW0015","LGL":"NW0005","ITS":"NW0501","IA":"NW0017"}
# note: CON background reports up via DMs; top bg managers report to Rahul/Gaurav (India) or Eleanor (London)

_cache = None
def background():
    global _cache
    if _cache: return _cache
    r = rng("people")
    ids = sorted(r.sample(range(2000, 5000), 2945))
    cast_names = set(NAME.values())
    used = set(cast_names)
    slots = []
    for u, n in UNIT_TARGET:
        slots += [u]*n
    while len(slots) < len(ids): slots.append("BFS")
    r.shuffle(slots)
    people = []
    for i, pid in zip(ids, slots):
        unit = pid
        if unit in ("BENCH","CON","BFS","HLC","CDP"):
            loc = r.choices(["BLR","PNQ","LON"], [55,35,10])[0]
        else:
            loc = r.choices(["BLR","PNQ","LON"], [70,22,8])[0]
        g = r.choices([g for g,_ in GRADE_W], [w for _,w in GRADE_W])[0]
        if unit == "BENCH" and g in ("G6","G7"): g = "G4"
        if unit in ("RMG","IA") and g in ("G6","G7"): g = "G5"
        if loc == "LON" and g in ("G1","G2"): g = "G4"
        for _ in range(200):
            if loc == "LON": nm = f"{r.choice(FIRST_UK)} {r.choice(LAST_UK)}"
            else: nm = f"{r.choice(FIRST_IN)} {r.choice(LAST_IN)}"
            if nm not in used: break
        else:
            nm = f"{nm} {i}"
        used.add(nm)
        title = FUNC_TITLES.get(unit, {}).get(g) or r.choice(TITLES.get(g, ["Engineer"]))
        if unit == "BENCH": title = TITLES[g][0] if g in TITLES else "Engineer"
        yr = r.choices([r.randint(2010,2021), r.randint(2022,2026)], [60,40])[0]
        if yr == 2026: joined = f"2026-0{r.randint(1,8)}-{r.randint(1,28):02d}"
        else: joined = f"{yr}-{r.randint(1,12):02d}-{r.randint(1,28):02d}"
        sk_pool = SKILLS.get(unit, []) + SKILLS["SHARED"]
        if unit == "BENCH": sk_pool = sum(SKILLS.values(), [])
        skills = r.sample(sk_pool, min(len(sk_pool), r.randint(3,6))) if unit in ("BFS","CON","HLC","CDP","BENCH") else []
        people.append({"id": f"NW{i:04d}", "name": nm, "grade": g, "title": title, "unit": unit, "loc": loc,
                       "joined": joined, "skills": skills, "mgr": None})
    # managers: each person -> random higher-grade person in same unit (G4+), else unit head
    by_unit = {}
    for p in people: by_unit.setdefault(p["unit"], []).append(p)
    gnum = lambda g: int(g[1:])
    for u, ps in by_unit.items():
        mgrs = [p for p in ps if gnum(p["grade"]) >= 4]
        for p in ps:
            cands = [m for m in mgrs if gnum(m["grade"]) > gnum(p["grade"])]
            if cands and gnum(p["grade"]) < 6:
                # prefer the next grade(s) up for a realistic tree
                best = min(gnum(m["grade"]) for m in cands)
                pool = [m for m in cands if gnum(m["grade"]) <= best+1]
                p["mgr"] = r.choice(pool)["id"]
            else:
                if u == "CON": p["mgr"] = "NW0013" if p["loc"] == "LON" else r.choice(["NW0011","NW1401","NW1101"])
                elif u == "BFS": p["mgr"] = r.choice(["NW0010","NW1202","NW1301"])
                elif u == "HLC": p["mgr"] = r.choice(["NW0012","NW1501","NW1601"])
                else: p["mgr"] = UNIT_HEAD.get(u, "NW0003")
            # pre-reorg unit (RTL/UKD) for CON people
        if u == "CON":
            for p in ps: p["pre_reorg_unit"] = "UKD" if p["loc"] == "LON" else "RTL"
    _cache = people
    return people

def named():
    out = []
    for (pid,nm,g,title,unit,loc,mgr,joined,sk) in CAST:
        out.append({"id":pid,"name":nm,"grade":g,"title":title,"unit":unit,"loc":loc,"mgr":mgr,"joined":joined,"skills":sk.split(";")})
    return out

def everyone():
    return named() + background()
