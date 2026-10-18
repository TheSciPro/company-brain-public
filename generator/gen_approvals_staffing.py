"""approvals/ (Concur / Salesforce deal desk-like) and staffing/ (Kantata-like PSA)."""
from common import *
from people import background

def step(n, approver, action, t, comment="", role=None, delegation=None):
    s = {"step":n,"approver_id":approver,"approver_name":NAME.get(approver,approver),"approver_role":role,"action":action,"acted_at":t,"comment":comment}
    if delegation: s["on_behalf_of"]=delegation
    return s

def apr(id_, sysname, typ, created, cls, requester, subject, steps, status, rule, links, amount=None, currency=None, acl=None, rfs=None, **kw):
    r = {**meta(sysname, id_, created, cls, acl=acl, updated_at=steps[-1]["acted_at"] if steps and steps[-1]["acted_at"] else created, restricted_fields=rfs),
         "type":typ,"requester_id":requester,"requester_name":NAME.get(requester,requester),"subject":subject,
         "amount":amount,"currency":currency,"matrix_rule":rule,"status":status,"steps":steps,"links":links}
    r.update(kw); return r

def run():
    A = []
    FINDM = "RESTRICTED-FIN-DM"
    # --- Deal desk: quote versions (12% draft superseded by 18%) ---
    A.append(apr("DQ-BRM-2026-001-v1","Salesforce CPQ (Deal Desk)","deal_quote",ts("2026-01-27","16:10:00"),FINDM,"NW0202",
        "Brightmart loyalty rebuild - proposed discount vs RC-2026 (DRAFT)",
        [step(1,"NW1101","reviewed",ts("2026-01-28","11:00:00"),"Shared with James for client temperature check")],
        "Superseded","POL-FIN-PRICE v3 §3.2 (10-15% => Unit Head)",["RFC-2026-002","ACC-BRM"],
        rfs=[rf("discount_pct",FINDM,"A draft quote existed and was superseded; value restricted"),rf("blended_rate_gbp",FINDM)],
        discount_pct=12.0, blended_rate_gbp=None, superseded_by="DQ-BRM-2026-001-v2", note="Never submitted for approval; superseded after Brightmart counter (CHT-2026-003)."))
    A.append(apr("DQ-BRM-2026-001-v2","Salesforce CPQ (Deal Desk)","deal_quote",ts("2026-02-05","18:30:00"),FINDM,"NW0202",
        "Brightmart loyalty rebuild - final pricing (exception PEX-2026-004)",
        [step(1,"NW0011","recommended",ts("2026-02-05","17:45:00"),"Pricing Committee recommends; >15% so CFO+CEO required",role="Unit Head")],
        "Approved (see APR-2026-0201)","POL-FIN-PRICE v3 §3.2 (>15% => CFO and CEO jointly + Pricing Committee note)",
        ["PEX-2026-004","APR-2026-0201","MOM-2026-007","DEC-2026-005"],
        rfs=[rf("discount_pct",FINDM,"An approved pricing exception exists (CFO+CEO, Feb 2026); ask DM or Finance"),rf("floor_margin_pct",FINDM)],
        discount_pct=18.0, floor_margin_pct=21.0, supersedes="DQ-BRM-2026-001-v1"))
    A.append(apr("APR-2026-0201","Salesforce Deal Desk Approvals","pricing_exception",ts("2026-02-06","10:00:00"),FINDM,"NW0202",
        "Approve PEX-2026-004: Brightmart RC-2026 less 18%",
        [step(1,"NW0002","approved",ts("2026-02-10","12:15:00"),"Approved with 21% floor and 2027 reprice checkpoint",role="CFO"),
         step(2,"NW0001","approved",ts("2026-02-10","15:40:00"),"Approved. Strategic UK logo.",role="CEO")],
        "Approved","POL-FIN-PRICE v3 §3.2 >15%: CFO and CEO jointly",["PEX-2026-004","MOM-2026-007","DEC-2026-005","ACC-BRM"],
        rfs=[rf("discount_pct",FINDM,"Exception exists; approvers CFO and CEO may be named")], discount_pct=18.0))
    # --- bench pull approvals ---
    A.append(apr("APR-2026-0216","Kantata PSA Approvals","bench_allocation",ts("2026-02-16","12:00:00"),"INTERNAL-ACCOUNT-BRM","NW0802",
        "Allocate 10 bench resources to Brightmart (STF-2026-0140)",
        [step(1,"NW0801","approved",ts("2026-02-17","10:30:00"),"Bench pull, RM Manager authority",role="RM Manager")],
        "Approved","POL-FIN-AUTH v5: Bench pull => RM Manager (G5)",["STF-2026-0140","BPL-2026-007","MOM-2026-009"]))
    A.append(apr("APR-2026-0219","Kantata PSA Approvals","active_project_release",ts("2026-02-18","09:30:00"),"INTERNAL-ACCOUNT-BRM","NW0801",
        "Release Varun Shah (NW1107) from Coastline SOW-CIN-03 to Brightmart",
        [step(1,"NW1202","approved",ts("2026-02-19","17:05:00"),"Approving under protest - Varun is Annex B key person, M3 due 30 Apr. 3 days handover only.",role="Releasing DM"),
         step(2,"NW1101","approved",ts("2026-02-19","18:20:00"),"Receiving DM",role="Receiving DM")],
        "Approved","POL-RMG-BENCH v2 §2.2 (releasing DM consent + 10 working days handover)",
        ["STF-2026-0146","CHT-2026-007","DEC-2026-008","SOW-CIN-03","POL-RMG-BENCH-v2"],
        handover_days_planned=3, handover_days_required=10, policy_breach="handover < 10 working days (v2 §2.2)",
        unit_head_consulted=False, legal_key_person_check=False, acl=CLASS_ACL["INTERNAL-ACCOUNT-BRM"]+["grp-acc-cin","NW0010"]))
    # --- access requests 24 Feb ---
    for i,pid in enumerate(BRM_TEAM):
        appr = "NW1105" if pid=="NW1109" else "NW1102"
        A.append(apr(f"APR-2026-0224-{i+1:02d}","ServiceNow Access Approvals","client_data_access",ts("2026-02-23","11:00:00"),"SECURITY-BRM",pid,
            f"Brightmart Citrix + BM-LOYALTY-PROD read access for {NAME[pid]}",
            [step(1,appr,"approved",ts("2026-02-24","16:00:00" if appr=="NW1102" else "18:40:00"),
                  "" if appr=="NW1102" else "Approved for Ritu (DLG-2026-004)",role="Project Manager",
                  delegation="NW1102" if appr=="NW1105" else None)],
            "Approved","POL-SEC-CDH v2 §4 (PM approves per-project access)",[f"ITT-2026-{301+i:04d}","POL-SEC-CDH-v2"]+(["DLG-2026-004"] if appr=="NW1105" else [])))
    # delegations
    A.append(apr("DLG-2026-004","ServiceNow Delegation Register","delegation",ts("2026-02-23","19:00:00"),"MGR","NW1102",
        "Ritu Agarwal delegates PM approvals (Brightmart access requests) to Mohit Saxena",
        [step(1,"NW1101","acknowledged",ts("2026-02-23","19:30:00"))],"Expired","POL-FIN-AUTH v5 §5.3",["POL-FIN-AUTH-v5"],
        delegate_id="NW1105",valid_from="2026-02-24",valid_to="2026-02-28",
        note="Mohit (G4) is lower grade than Ritu (G5) - only permitted for access approvals per DLG scope; expired 28 Feb. No delegation existed in June."))
    A.append(apr("DLG-2026-011","Concur Delegation","delegation",ts("2026-07-03","10:00:00"),"MGR","NW1101",
        "Siddharth Chauhan delegates expense approvals to Gaurav Kohli (G6) during leave",
        [step(1,"NW0013","acknowledged",ts("2026-07-03","12:00:00"))],"Expired","POL-FIN-AUTH v5 §5.3",["POL-FIN-AUTH-v5"],
        delegate_id="NW1401",valid_from="2026-07-27",valid_to="2026-07-31"))
    # travel / expense pre & post reorg
    PERS = lambda req, extra=[]: [req]+extra
    A.append(apr("APR-2026-0316","SAP Concur Travel","international_travel",ts("2026-03-16","09:20:00"),"PERSONAL","NW1107",
        "London, Brightmart discovery workshop, 12 days (23 Mar - 3 Apr)",
        [step(1,"NW1101","approved",ts("2026-03-16","14:00:00"),"Client-billable confirmed per SOW-BRM-01 §6",role="G6 / DM"),
         step(2,"NW0011","approved",ts("2026-03-17","10:10:00"),"",role="Unit Head (Retail, pre-reorg)")],
        "Approved","POL-HR-TRAVEL v3 §3.2: G6 + Unit Head (G8); <30 days no immigration review",["POL-HR-TRAVEL-v3","SOW-BRM-01"],
        amount=385000,currency="INR",acl=["NW1107","NW1101","NW0011","grp-finance"],billable=True,duration_days=12))
    A.append(apr("APR-2026-0318","SAP Concur Expense","expense",ts("2026-03-18","11:00:00"),"PERSONAL","NW1101",
        "Client dinner + team offsite, Brightmart kick-off",
        [step(1,"NW0011","approved",ts("2026-03-19","09:00:00"),"",role="Line manager (pre-reorg)")],
        "Approved","POL-FIN-AUTH v5: expense <= ₹2,00,000 => G6+ in reporting line (requester is G6, so manager)",["POL-FIN-AUTH-v5"],
        amount=60000,currency="INR",acl=["NW1101","NW0011","grp-finance"]))
    A.append(apr("APR-2026-0420","SAP Concur Expense","expense",ts("2026-04-20","10:00:00"),"PERSONAL","NW1115",
        "London client workshop materials & travel (UK Ltd)",
        [step(1,"NW1101","approved",ts("2026-04-20","15:00:00"),"",role="Line manager G6 (UK limit B £2,000)")],
        "Approved","POL-FIN-AUTH v5 UK: A=£250 line manager, B=£2,000 G6",["POL-FIN-AUTH-v5"],amount=1200,currency="GBP",acl=["NW1115","NW1101","grp-finance"]))
    A.append(apr("APR-2026-0512","SAP Concur Expense","expense",ts("2026-05-12","10:30:00"),"PERSONAL","NW1105",
        "Kafka Summit London + certification (team capability)",
        [step(1,"NW1101","approved",ts("2026-05-12","17:00:00"),"Within G6 limit. Above ₹2L would go to Eleanor (Head Consumer) not Rahul.",role="G6 in reporting line")],
        "Approved","POL-FIN-AUTH v5: ₹25k-₹2L => G6+ in reporting line; > ₹2L => G8 Unit Head + Financial Controller",["POL-FIN-AUTH-v5","ORG-2026-05"],
        amount=180000,currency="INR",acl=["NW1105","NW1101","NW0013","grp-finance"],escalation_path=["NW1101","NW0013","NW0015"]))
    A.append(apr("APR-2026-0602","SAP Concur Expense","expense",ts("2026-06-02","12:00:00"),"PERSONAL","NW1101",
        "Brightmart QBR hosting, Bangalore",
        [step(1,"NW0013","approved",ts("2026-06-03","09:30:00"),"",role="Line manager (post-reorg)")],
        "Approved","POL-FIN-AUTH v5 §5.2: reporting line as of request date (manager = Eleanor since 2026-05-01)",["POL-FIN-AUTH-v5","ORG-2026-05"],
        amount=55000,currency="INR",acl=["NW1101","NW0013","grp-finance"]))
    A.append(apr("APR-2026-0815","SAP Concur Expense","expense",ts("2026-08-15","10:00:00"),"PERSONAL","NW1112",
        "Late-night support cab + meals (peak prep)",
        [step(1,"NW1105","approved",ts("2026-08-15","18:00:00"),"",role="Line manager G4")],
        "Approved","POL-FIN-AUTH v5: <= ₹25,000 line manager (G4+)",["POL-FIN-AUTH-v5"],amount=18000,currency="INR",acl=["NW1112","NW1105","grp-finance"]))
    A.append(apr("APR-2026-0909","SAP Concur Expense","expense",ts("2026-09-09","11:00:00"),"PERSONAL","NW1110",
        "Load-testing SaaS licences (annual) - paid on corporate card",
        [step(1,"NW1105","forwarded",ts("2026-09-09","15:00:00"),"Above my limit",role="Line manager"),
         step(2,"NW1101","forwarded",ts("2026-09-10","10:00:00"),"Above ₹2L",role="G6"),
         step(3,"NW0013","approved",ts("2026-09-11","09:00:00"),"",role="Unit Head (Consumer)"),
         step(4,"NW0015","approved",ts("2026-09-11","16:00:00"),"",role="Financial Controller")],
        "Approved","POL-FIN-AUTH v5: > ₹2,00,000 => G8 Unit Head + Financial Controller",["POL-FIN-AUTH-v5","ORG-2026-05"],
        amount=240000,currency="INR",acl=["NW1110","NW1105","NW1101","NW0013","grp-finance"]))
    A.append(apr("APR-2026-0706","Workday Absence","leave",ts("2026-06-19","10:00:00"),"PERSONAL","NW1102",
        "Earned leave 12 working days (6 Jul - 21 Jul)",
        [step(1,"NW1101","approved",ts("2026-06-20","11:00:00"),"Requester is the PM, so line-manager approval suffices",role="Line manager")],
        "Approved","POL-HR-LEAVE v4 §4.3 (6-15 days: line manager + PM); §4.4 15 days' notice",["POL-HR-LEAVE-v4"],
        acl=["NW1102","NW1101","grp-hr"],leave_type="Earned",start="2026-07-06",end="2026-07-21",working_days=12,applied_on="2026-06-19",notice_days=17))
    A.append(apr("APR-2026-0721","Coupa Spend Approvals","unbudgeted_cloud_spend",ts("2026-07-21","10:00:00"),"RESTRICTED-FIN-DM","NW1112",
        "Azure AKS + Event Hubs scale-up for Brightmart peak prep (unbudgeted)",
        [step(1,"NW1105","endorsed",ts("2026-07-21","12:00:00"),"Technical need confirmed",role="Acting Architect"),
         step(2,"NW1101","endorsed",ts("2026-07-21","15:00:00"),"Above DM limit ₹1L",role="DM"),
         step(3,"NW0013","approved",ts("2026-07-22","10:00:00"),"Within Unit Head authority (₹1L-₹10L)",role="Unit Head (Consumer)")],
        "Approved","POL-FIN-AUTH v5: hotfix/unbudgeted cloud ₹1L-₹10L => Unit Head",["POL-FIN-AUTH-v5","ORG-2026-05"],amount=650000,currency="INR"))
    A.append(apr("APR-2026-0918","SAP Concur Travel","international_travel",ts("2026-09-18","10:00:00"),"PERSONAL","NW1103",
        "London, Brightmart steering committee + remediation review, 10 days (5-14 Oct)",
        [step(1,"NW1101","pending",None,"Awaiting billability confirmation",role="G6 / DM"),
         step(2,"NW0013","not_started",None,"",role="Unit Head (Consumer)")],
        "Pending","POL-HR-TRAVEL v3 §3.2/3.3: G6 + Unit Head; DM confirms billability; <30 days no CHRO",["POL-HR-TRAVEL-v3","ORG-2026-05"],
        amount=410000,currency="INR",acl=["NW1103","NW1101","NW0013","grp-finance"],duration_days=10,billable="tbc"))
    # finance records
    A.append(apr("APR-2026-0331","Finance Margin Governance","margin_recovery_plan",ts("2026-03-31","17:00:00"),"RESTRICTED-FIN","NW0201",
        "Brightmart Q1 margin 20.4% below 21% floor - recovery plan",
        [step(1,"NW0015","approved",ts("2026-04-02","11:00:00"),"Quarterly review per POL-FIN-PRICE §3.3",role="Financial Controller")],
        "Approved","POL-FIN-PRICE v3 §3.3 (<22% margin needs recovery plan)",["PEX-2026-004","MOM-2026-015"],
        acl=CLASS_ACL["RESTRICTED-FIN"]+["NW1101"],q1_margin_pct=20.4,floor_margin_pct=21.0,
        rfs=[rf("q1_margin_pct","RESTRICTED-FIN")]))
    A.append(apr("APR-2026-0506","Finance Margin Governance","service_credit",ts("2026-05-06","12:00:00"),"RESTRICTED-FIN","NW1202",
        "Coastline Insurance service credit - M3 missed (SOW-CIN-03 §8)",
        [step(1,"NW0015","approved",ts("2026-05-06","16:00:00"),"Recorded against Q2 revenue",role="Financial Controller")],
        "Approved","SOW-CIN-03 §8 service credit",["ESC-2026-002","SOW-CIN-03","STF-2026-0146"],amount=48000,currency="USD",
        acl=CLASS_ACL["RESTRICTED-FIN"]+["NW1202","NW0010"],rfs=[rf("amount","RESTRICTED-FIN","PM may be told 'a service credit was applied'")]))
    A.append(apr("APR-2026-0610","Client Governance Log","client_approval_key_person",ts("2026-05-20","18:00:00"),"CLIENT-CONF-BRM","NW1101",
        "Brightmart consent: replace named architect (MSA §9) Ananya Krishnan -> Mohit Saxena (acting)",
        [step(1,"EXT-BM-01","approved",ts("2026-06-10","16:00:00"),"Approved; noted notice was 21 days vs 30 required. Expect a permanent architect plan.",role="Client CIO")],
        "Approved","MSA-BRM-2026 §9 (30 days' notice + client approval)",["MSA-BRM-2026","CHT-2026-022","STF-2026-0388","STF-2026-0389"],
        notice_given="2026-05-20",client_approved="2026-06-10",effective="2026-06-15",notice_days=21,contract_required_notice_days=30,
        note="Notice given 2026-05-20; client approval 2026-06-10 = 21 days of notice before approval vs 30 required by MSA §9 (late)."))
    # access reviews (monthly per POL-SEC-CDH §4) - June missing on purpose
    reviews = [("APR-2026-0330","2026-03","NW1102","2026-03-30"),("APR-2026-0429","2026-04","NW1102","2026-04-29"),
               ("APR-2026-0529","2026-05","NW1102","2026-05-29"),("APR-2026-0729","2026-07","NW1102","2026-07-29"),
               ("APR-2026-0908","2026-08 (+ June catch-up)","NW1103","2026-09-08")]
    for rid,period,who,d in reviews:
        A.append(apr(rid,"ServiceNow Access Certification","access_review",ts(d,"17:00:00"),"SECURITY-BRM",who,
            f"Monthly access review - Brightmart client data - {period}",
            [step(1,who,"certified",ts(d,"17:00:00"),"14 accounts reviewed; " + ("1 suspension (NW1109) confirmed; June gap documented" if rid=="APR-2026-0908" else "no changes"),role="Project Manager")],
            "Completed","POL-SEC-CDH v2 §4 / ISO A.5.18",["POL-SEC-CDH-v2","CTL-A.5.18"]+(["AUD-2026-02"] if rid=="APR-2026-0908" else []),
            review_period=period))
    # background noise approvals (~2,500)
    r = rng("approvals"); bg = background()
    types = [("expense","SAP Concur Expense"),("domestic_travel","SAP Concur Travel"),("leave","Workday Absence")]
    for i in range(2500):
        p = r.choice(bg); t,s = r.choice(types)
        d = f"2026-{r.randint(1,9):02d}-{r.randint(1,28):02d}"
        amt = r.choice([r.randint(800,25000), r.randint(25000,90000)]) if t!="leave" else None
        A.append(apr(f"APR-BG-{i+1:05d}",s,t,ts(d),"PERSONAL",p["id"],f"{t.replace('_',' ').title()} request",
            [step(1,p["mgr"],r.choice(["approved"]*9+["rejected"]),ts(d,"18:00:00"))],"Approved","POL-FIN-AUTH v5",[],
            amount=amt,currency="INR" if amt else None,acl=[p["id"],p["mgr"],"grp-finance"],background=True))
    for a in A:
        if a["classification"]=="PERSONAL" and not a["acl"]: a["acl"]=[a["requester_id"]]+[s["approver_id"] for s in a["steps"]]
    named = [a for a in A if not a.get("background")]
    write_json("approvals/approval_records.json", named)
    write_jsonl("approvals/background_approvals.jsonl", [a for a in A if a.get("background")])
    # pricing exceptions register
    pex = [{**meta("Finance Exception Register","PEX-2026-004",ts("2026-02-10","16:00:00"),"RESTRICTED-FIN",acl=CLASS_ACL["RESTRICTED-FIN"]+["grp-pricing-desk","grp-acc-brm-dm","NW0011","NW0013"],
              restricted_fields=[rf("discount_pct","RESTRICTED-FIN-DM","Existence + approvers (CFO, CEO, Feb 2026) may be disclosed to Brightmart PM; never % / rates / margin"),
                                 rf("effective_rates_gbp","RESTRICTED-FIN-DM"),rf("floor_margin_pct","RESTRICTED-FIN-DM")],
              existence_visible_to=["grp-acc-brm","grp-mgr"]),
            "title":"Pricing Exception: Brightmart UK","account":"ACC-BRM","rate_card":"RC-2026","discount_pct":18.0,
            "effective_rates_gbp":{"offshore":{"G1":14.76,"G2":19.68,"G3":26.24,"G4":32.80,"G5":39.36,"G6":47.56},
                                   "onsite_london":{"G3":61.50,"G4":72.16,"G5":80.36,"G6":94.30,"G7":114.80}},
            "floor_margin_pct":21.0,"policy_target_margin_pct":28.0,"approvers":["NW0002","NW0001"],"approved_on":"2026-02-10",
            "approval_record":"APR-2026-0201","pricing_committee_note":"MOM-2026-007","decision":"DEC-2026-005",
            "valid_for":"Initial contract term (POL-FIN-PRICE v3 §3.4): 2026-02-12 to 2029-02-11; renegotiate at renewal",
            "checkpoints":["2027-02 reprice checkpoint (DEC-2026-005)"],"supersedes_draft":"RFC-2026-002 / DQ-BRM-2026-001-v1 (12%, never approved)"},
           {**meta("Finance Exception Register","PEX-2026-002",ts("2026-01-14"),"RESTRICTED-FIN"),
            "title":"Pricing Exception: Veridian Pharma onsite blend (decoy)","account":"ACC-VER","discount_pct":7.0,
            "approvers":["NW1601"],"approved_on":"2026-01-14","note":"≤10%, DM authority. Unrelated to Brightmart."}]
    for i in range(6):
        pex.append({**meta("Finance Exception Register",f"PEX-2025-{i+11:03d}",ts(f"2025-{i+3:02d}-10"),"RESTRICTED-FIN"),
            "title":f"Pricing Exception: CL-{100+i*7:03d}","account":f"CL-{100+i*7:03d}","discount_pct":[4,6,8,11,9,5][i],"background":True})
    write_json("approvals/pricing_exceptions.json", pex)
    staffing()
    return {"approvals_named":len(named),"approvals_bg":2500,"pex":len(pex)}

def staffing():
    r = rng("staffing"); bg = background()
    allocs=[]
    def al(id_, pid, acc, proj, start, end, pct, kind, role, status, cls="INTERNAL-ACCOUNT-BRM", **kw):
        allocs.append({**meta("Kantata PSA",id_,ts(start),cls,acl=None if cls!="MGR" else None),"resource_id":pid,"resource_name":NAME.get(pid,pid),
            "account":acc,"project":proj,"role":role,"start":start,"end":end,"allocation_pct":pct,"booking_type":kind,"status":status, **kw})
    prev = {"NW1106":("CL-212","Background retail analytics (ending 2026-02-20)"),"NW1107":("ACC-CIN","SOW-CIN-03 ClaimCenter migration")}
    for i,pid in enumerate(BRM_TEAM):
        sid=STF_ID[pid]
        frm = prev.get(pid,("BENCH","Bench"))
        al(sid,pid,"ACC-BRM","PRJ-BRM-LOY",  "2026-02-23", None if pid!="NW1109" else None, 100,"hard",CAST_BY_ID[pid][3],"Active",
           from_assignment=frm[0], from_note=frm[1], demand="STF-2026-0140", bench_pull="BPL-2026-007",
           release_approval="APR-2026-0219" if pid=="NW1107" else None,
           pulled_from_active_project= pid in prev)
    al("STF-2026-0146-PRE","NW1107","ACC-CIN","PRJ-CIN-CC","2025-06-02","2026-02-22",100,"hard","Senior Engineer (Annex B key person)","Closed - early release",
       cls="CLIENT-CONF-CIN", planned_end="2026-06-30", handover_days=3, completion_pct_at_release=60)
    al("STF-2026-0110","NW1104","ACC-BRM","PRJ-BRM-LOY","2026-01-20","2026-06-14",100,"hard","Solution Architect (MSA §9 named key person)","Closed")
    al("STF-2026-0388","NW1104","ACC-HBK","PRJ-HBK-PAY","2026-06-15",None,100,"hard","Solution Architect","Active",cls="MGR",
       client_consent="APR-2026-0610", handover_minutes="MOM-2026-024")
    al("STF-2026-0389","NW1105","ACC-BRM","PRJ-BRM-LOY","2026-06-15",None,100,"hard","Acting Architect (formal owner)","Active",
       note="Replaces Ananya under MSA §9; acting until permanent architect named")
    al("STF-2026-0105","NW1105","ACC-BRM","PRJ-BRM-LOY","2026-02-01","2026-06-14",100,"hard","Tech Lead","Closed")
    al("STF-2026-0101","NW1101","ACC-BRM","PRJ-BRM-LOY","2026-02-01",None,60,"hard","Delivery Manager","Active")
    al("STF-2026-0102","NW1102","ACC-BRM","PRJ-BRM-LOY","2026-02-13","2026-08-23",100,"hard","Project Manager","Closed")
    al("STF-2026-0512","NW1102","ACC-MED","PRJ-MED-HIS","2026-08-24",None,100,"hard","Project Manager","Active",cls="MGR",approved_by=["NW1101","NW0014","NW1501"],reason="DEC-2026-034")
    al("STF-2026-0513","NW1103","ACC-BRM","PRJ-BRM-LOY","2026-08-24",None,100,"hard","Project Manager","Active",approved_by=["NW1101","NW0014"],reason="DEC-2026-034",
       previous="ACC-HBK sub-PM (PRJ-HBK-PAY) until 2026-08-21")
    al("STF-2026-0090","NW1103","ACC-HBK","PRJ-HBK-PAY","2025-04-01","2026-08-21",100,"hard","Sub-PM","Closed",cls="MGR")
    al("STF-2026-0103","NW1118","ACC-BRM","PRJ-BRM-LOY","2026-01-08",None,50,"hard","Client Partner","Active")
    al("STF-2026-0201","NW1801","BENCH","BENCH","2026-01-05",None,0,"none","Bench","Active",cls="MGR",bench_days_as_of_demo=266,
       note="Proposed for Brightmart (Kafka) but not selected; no loyalty-service history")
    al("STF-2026-0202","NW1801","ACC-BRM","PRJ-BRM-LOY","2026-02-23","2026-03-31",50,"soft","Kafka engineer (tentative)","Cancelled",cls="MGR")
    for pid,acc,proj in [("NW1201","ACC-CIN","PRJ-CIN-CC"),("NW1202","ACC-CIN","PRJ-CIN-CC"),("NW1301","ACC-HBK","PRJ-HBK-PAY"),("NW1302","ACC-HBK","PRJ-HBK-PAY"),
                          ("NW1401","ACC-ZEN","PRJ-ZEN-ECOM"),("NW1501","ACC-MED","PRJ-MED-HIS"),("NW1601","ACC-VER","PRJ-VER-GXP")]:
        al(f"STF-2026-{r.randint(20,89):04d}-{pid}",pid,acc,proj,"2026-01-01",None,100,"hard",CAST_BY_ID[pid][3],"Active",cls="MGR")
    # background teams on other accounts (~40 people)
    pool = [p for p in bg if p["unit"] in ("BFS","CON","HLC") and p["grade"] in ("G1","G2","G3","G4")]
    accs = [("ACC-CIN","PRJ-CIN-CC","BFS"),("ACC-HBK","PRJ-HBK-PAY","BFS"),("ACC-ZEN","PRJ-ZEN-ECOM","CON"),("ACC-MED","PRJ-MED-HIS","HLC"),("ACC-VER","PRJ-VER-GXP","HLC")]
    k=0
    for acc,proj,u in accs:
        for p in [q for q in pool if q["unit"]==u][:8]:
            k+=1; pct=r.choice([100,100,100,50]); kind=r.choice(["hard"]*4+["soft"])
            al(f"STF-2026-{600+k:04d}",p["id"],acc,proj,f"2026-{r.randint(1,4):02d}-01",r.choice([None,"2026-12-31"]),pct,kind,p["title"],"Active",cls="MGR")
        pool=[q for q in pool if q["id"] not in {a["resource_id"] for a in allocs}]
    write_json("staffing/allocations.json", allocs)
    write_json("staffing/demands_and_bench_pulls.json", [
      {**meta("Kantata PSA","STF-2026-0140",ts("2026-02-13"),"INTERNAL-ACCOUNT-BRM"),"type":"resource_demand","raised_by":"NW1101","account":"ACC-BRM",
       "need":"12 staff by 2026-02-23 (Kafka, Azure Functions, Java, React, Spark, QA, DevOps, BA onsite)","status":"Fulfilled","fulfilled_by":"BPL-2026-007"},
      {**meta("Kantata PSA","BPL-2026-007",ts("2026-02-16"),"INTERNAL-ACCOUNT-BRM",acl=CLASS_ACL["INTERNAL-ACCOUNT-BRM"]+["grp-acc-cin"]),"type":"bench_pull","proposed_by":["NW0801","NW0802"],
       "demand":"STF-2026-0140","resources":BRM_TEAM,"from_bench":[p for p in BRM_TEAM if p not in ("NW1106","NW1107")],
       "from_active_projects":[{"resource":"NW1106","from":"CL-212","note":"background project ending 2026-02-20 anyway"},
                               {"resource":"NW1107","from":"ACC-CIN / SOW-CIN-03","note":"Annex B key person; M3 due 2026-04-30","release_approval":"APR-2026-0219"}],
       "approval":"APR-2026-0216","minutes":"MOM-2026-009","policy_in_force":"POL-RMG-BENCH v2"}])
    ms = [("MS-CIN-M3","ACC-CIN","M3 ClaimCenter claims-module cutover","2026-04-30","2026-05-21","Missed (3 weeks late)",["ESC-2026-002","APR-2026-0506"],"CLIENT-CONF-CIN"),
          ("MS-CIN-M2","ACC-CIN","M2 Policy data migration","2026-01-31","2026-01-30","Met",[],"CLIENT-CONF-CIN"),
          ("MS-BRM-M1","ACC-BRM","M1 Discovery & architecture sign-off","2026-04-10","2026-04-09","Met",["RFC-2026-010"],"INTERNAL-ACCOUNT-BRM"),
          ("MS-BRM-M2","ACC-BRM","M2 Loyalty-service MVP (points accrual)","2026-07-15","2026-07-17","Met (2 days late, no credit)",[],"INTERNAL-ACCOUNT-BRM"),
          ("MS-BRM-M3","ACC-BRM","M3 Peak-ready release (pre-freeze)","2026-11-10",None,"On track - at risk (remediation work)",["ESC-2026-005"],"INTERNAL-ACCOUNT-BRM"),
          ("MS-BRM-REM","ACC-BRM","Remediation plan delivery to client (ESC-2026-005, 14 days)","2026-08-20","2026-08-18","Met",["DEC-2026-036"],"INTERNAL-ACCOUNT-BRM")]
    write_json("staffing/milestones.json",[{**meta("Kantata PSA",i,ts("2026-01-15"),c,updated_at=ts(act or DEMO_DATE)),"account":a,"name":n,"due":d,"actual":act,"status":s,"links":l} for i,a,n,d,act,s,l,c in ms])
