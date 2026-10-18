"""security_grc/ (Vanta-like GRC + Okta System Log + ServiceNow ITSM + Intune devices)."""
import uuid
from common import *
from people import background

SEC="SECURITY"; SB="SECURITY-BRM"
def u(r): return str(uuid.UUID(int=r.getrandbits(128)))

def run():
    r = rng("security")
    # ---------- controls ----------
    C = [("A.5.1","Policies for information security","NW0006","ISMS-SOA-2026.1"),
         ("A.5.9","Inventory of information and other associated assets","NW1701","POL-SEC-CDH-v2"),
         ("A.5.15","Access control","NW0501","POL-SEC-CDH-v2 §4"),
         ("A.5.18","Access rights (provisioning/review)","NW0501","POL-SEC-CDH-v2 §4"),
         ("A.5.23","Information security for use of cloud services","NW0007","POL-SEC-CDH-v2"),
         ("A.5.24","Information security incident management planning and preparation","NW0501","POL-SEC-IR-v2"),
         ("A.5.26","Response to information security incidents","NW0501","POL-SEC-IR-v2 §4"),
         ("A.5.34","Privacy and protection of PII","NW0402","ADD-BRM-GDPR-01"),
         ("A.6.1","Screening","NW0004","POL-HR (onboarding)"),
         ("A.6.3","Information security awareness, education and training","NW0502","—"),
         ("A.6.7","Remote working","NW0006","POL-HR-WFH-v3 §2.4"),
         ("A.8.1","User endpoint devices","NW1701","POL-SEC-CDH-v2 §2"),
         ("A.8.5","Secure authentication","NW0501","—"),
         ("A.8.12","Data leakage prevention","NW0503","POL-SEC-CDH-v2 §3"),
         ("A.8.16","Monitoring activities","NW0503","—")]
    in_soa = {"A.5.15","A.5.18","A.5.24","A.5.34","A.6.3","A.6.7","A.8.1","A.8.12","A.8.16"}
    status = {"A.8.1":"Nonconformity (Major, AUD-2026-01)","A.6.7":"Nonconformity (Major, AUD-2026-01)","A.5.18":"Nonconformity (Minor, AUD-2026-02)","A.6.3":"Observation (AUD-2026-03)"}
    controls=[{**meta("Vanta (GRC)",f"CTL-{c}",ts("2026-01-15"),"SECURITY",updated_at=ts("2026-09-22")),
        "framework":"ISO/IEC 27001:2022 Annex A","control_ref":c,"name":n,"owner_id":o,"owner_name":NAME[o],
        "linked_policy":pol,"soa_version":"2026.1","in_spec_subset":c in in_soa,"applicable":True,
        "status":status.get(c,"Operating"),
        **({"operator_note":"Per-project access reviews delegated to account PMs; Brightmart PM = NW1103 Nikhil Bose from 2026-08-24 (previously NW1102)"} if c=="A.5.18" else {})}
        for c,n,o,pol in C]
    write_json("security_grc/controls.json", controls)
    # ---------- Okta System Log ----------
    team = ["NW1101","NW1102","NW1105"]+BRM_TEAM
    ev=[]
    def okta(pid, when, etype, msg, ip, managed, outcome="SUCCESS", device_id=None, device_name=None, reason=None, city="Bangalore", isp="Northwind ODC-2", extra=None):
        e = {"uuid":u(r),"published":when,"eventType":etype,"version":"0","severity":"INFO" if outcome=="SUCCESS" and managed else "WARN",
             "displayMessage":msg,
             "actor":{"id":f"00u{pid.lower()}","type":"User","alternateId":login(pid),"displayName":NAME[pid],"detailEntry":{"employeeNumber":pid}},
             "client":{"userAgent":{"rawUserAgent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Citrix Workspace/2402","os":"Windows 10","browser":"CHROME"},
                       "zone":"null","device":"Computer","ipAddress":ip,
                       "geographicalContext":{"city":city,"state":"Karnataka" if city=="Bangalore" else None,"country":"India"}},
             "device":{"id":device_id,"name":device_name,"os_platform":"WINDOWS","managed":managed,"registered":managed,
                       "disk_encryption_type":"FULL" if managed else "NONE","secure_hardware_present":managed},
             "outcome":{"result":outcome,"reason":reason},
             "target":[{"id":"0oabmcitrix","type":"AppInstance","alternateId":"Brightmart Citrix Gateway (BM-LOYALTY-PROD)","displayName":"Brightmart Citrix"}],
             "securityContext":{"isp":isp,"isProxy":False},
             "debugContext":{"debugData":{"requestUri":"/app/citrix_netscaler/sso/saml","deviceCompliance":"COMPLIANT" if managed else "UNKNOWN",
                                         "policyEvaluated":"BM-Citrix-AccessPolicy v1 (device assurance: NOT ENFORCED)"}},
             "x_northwind":meta("Okta System Log",None,when,SB,restricted_fields=[rf("actor.displayName","SECURITY","External auditors see actor.detailEntry.employeeNumber only")])}
        e["x_northwind"]["id"]=f"OKTA-{e['uuid'][:8]}"
        if extra: e["x_northwind"].update(extra)
        ev.append(e)
    days = ["2026-07-0%d"%d for d in range(1,10)]+["2026-07-1%d"%d for d in range(0,10)]+["2026-07-2%d"%d for d in range(0,8)]
    devs = {p:(f"guid-{p.lower()}-lt", f"NW-LT-{p[2:]}") for p in team}
    while len(ev)<96:
        p=r.choice(team); d=r.choice(days)
        if NAME[p]=="Ritu Agarwal" and "2026-07-06"<=d<="2026-07-21": continue
        okta(p, ts(d, f"{r.randint(9,19):02d}:{r.randint(0,59):02d}:{r.randint(0,59):02d}"), "user.authentication.sso",
             "User single sign on to app", f"203.0.113.{r.randint(10,60)}", True, device_id=devs[p][0], device_name=devs[p][1])
    # the incident session (14 Jul, late night, home Wi-Fi, personal laptop)
    okta("NW1109", ts("2026-07-14","22:47:13"), "user.authentication.sso","User single sign on to app","49.207.61.188",False,
         device_id=None, device_name="DESKTOP-7QH2LMK", city="Bangalore", isp="ACT Fibernet (residential)",
         extra={"id":"OKTA-EVT-20260714-2247","incident":"INC-2026-014","note":"Device not in Intune inventory; access succeeded because device assurance was not enforced on Citrix gateway"})
    okta("NW1109", ts("2026-07-14","22:49:02"), "app.generic.session.start","Citrix session started: BM-LOYALTY-PROD members table (pipeline job ldp-accrual-nightly)","49.207.61.188",False,
         device_name="DESKTOP-7QH2LMK", city="Bangalore", isp="ACT Fibernet (residential)", extra={"id":"OKTA-EVT-20260714-2249","incident":"INC-2026-014"})
    okta("NW1109", ts("2026-07-15","01:38:40"), "user.session.end","Citrix session ended (2h49m)","49.207.61.188",False,
         device_name="DESKTOP-7QH2LMK", city="Bangalore", isp="ACT Fibernet (residential)", extra={"id":"OKTA-EVT-20260715-0138","incident":"INC-2026-014"})
    okta("NW1109", ts("2026-08-03","11:05:00"), "user.lifecycle.suspend","App assignment suspended: Brightmart Citrix","203.0.113.5",True,
         device_id=devs["NW1109"][0], device_name=devs["NW1109"][1], extra={"id":"OKTA-EVT-20260803-SUSP","incident":"INC-2026-014","actor_override":"NW0501"})
    okta("NW1109", ts("2026-09-01","10:20:00"), "application.user_membership.add","App assignment restored: Brightmart Citrix (device NW-LT-1109 compliant)","203.0.113.5",True,
         device_id=devs["NW1109"][0], device_name=devs["NW1109"][1], extra={"id":"OKTA-EVT-20260901-REST","ticket":"ITT-2026-2301"})
    okta("NW1110", ts("2026-09-16","20:10:00"), "user.authentication.sso","Denied: device not compliant (policy v2 enforced on 80% of gateway nodes)","49.205.12.7",False,
         outcome="DENY", reason="Device assurance policy BM-Citrix v2", device_name="KIRAN-HOME-PC", isp="Airtel (residential)", extra={"id":"OKTA-EVT-20260916-DENY","capa":"CAPA-2026-014-1"})
    ev.sort(key=lambda e:e["published"])
    write_json("security_grc/okta_system_log.json", ev)
    # ---------- Intune device inventory (subset) ----------
    devices=[{**meta("Microsoft Intune",f"DEV-{devs[p][1]}",ts("2026-02-20"),SEC),"deviceName":devs[p][1],"userPrincipalName":login(p),"employeeNumber":p,
              "complianceState":"compliant","isEncrypted":True,"managementAgent":"mdm","enrolledDateTime":ts("2026-02-20")} for p in team]
    write_json("security_grc/intune_devices.json", devices)
    # ---------- ITSM tickets ----------
    T=[]
    for i,p in enumerate(BRM_TEAM):
        T.append({**meta("ServiceNow",f"ITT-2026-{301+i:04d}",ts("2026-02-23","11:00:00"),SB,updated_at=ts("2026-02-24","18:40:00")),
                  "category":"Access Request","requested_for":p,"short_description":f"Brightmart Citrix + BM-LOYALTY-PROD read for {NAME[p]}",
                  "approval":f"APR-2026-0224-{i+1:02d}","state":"Closed Complete"})
    T += [
     {**meta("ServiceNow","ITT-2026-2210",ts("2026-08-14","10:00:00"),SB,updated_at=ts("2026-09-15","18:00:00")),"category":"Change / CAPA",
      "short_description":"CAPA-1: Enforce device compliance (Intune) via Okta device assurance on Brightmart Citrix gateway","assigned_to":"NW1701",
      "due_date":"2026-09-15","state":"Work in Progress","percent_complete":80,"overdue":True,"capa":"CAPA-2026-014-1","finding":"AUD-2026-01",
      "work_notes":[{"at":ts("2026-08-20"),"by":"NW1701","note":"Policy v2 piloted on 2 of 10 gateway nodes"},
                    {"at":ts("2026-09-15","18:00:00"),"by":"NW1701","note":"80% of nodes enforced. Remaining 2 nodes blocked by Brightmart change freeze window; new ETA 2026-10-09"}]},
     {**meta("ServiceNow","ITT-2026-2301",ts("2026-08-29","15:00:00"),SB,updated_at=ts("2026-09-01","10:20:00")),"category":"Access Request",
      "requested_for":"NW1109","short_description":"Restore Brightmart Citrix access for NW1109 after re-training; device NW-LT-1109 enrolled & compliant",
      "approved_by":["NW1103","NW0501"],"state":"Closed Complete","restored_on":"2026-09-01",
      "related":["SEC-2026-0077","HRC-2026-0031"],"restricted_fields":[rf("related[HRC-2026-0031]","RESTRICTED-HR","PM sees: restricted HR context exists; contact HRBP Pooja Reddy")]},
     {**meta("ServiceNow","ITT-2026-2215",ts("2026-08-14"),SB),"category":"CAPA","short_description":"CAPA-2: IR runbook - client-specific notification clock lookup (DPA 24h)","assigned_to":"NW0501","due_date":"2026-08-31","state":"Closed Complete"},
     {**meta("ServiceNow","ITT-2026-2216",ts("2026-08-14"),SB),"category":"CAPA","short_description":"CAPA-3: Monthly access-review calendar with mandatory delegate","assigned_to":"NW1103","due_date":"2026-09-30","state":"Work in Progress","note":"Reassigned from NW1102 on 2026-08-26 (MOM-2026-038)"},
     {**meta("ServiceNow","ITT-2026-2217",ts("2026-08-14"),SB),"category":"CAPA","short_description":"CAPA-4: Complete A.6.3 security training for 3 Brightmart staff","assigned_to":"NW1117","due_date":"2026-08-31","state":"Closed Complete"}]
    bg=background()
    for i in range(120):
        p=r.choice(bg); d=f"2026-{r.randint(1,9):02d}-{r.randint(1,28):02d}"
        T.append({**meta("ServiceNow",f"ITT-2026-{3000+i}",ts(d),"PUBLIC-INT",acl=[p["id"],"grp-security"]),"category":r.choice(["Laptop","VPN","Password","Software"]),
                  "requested_for":p["id"],"short_description":"Background IT request","state":"Closed Complete","background":True})
    write_json("security_grc/itsm_tickets.json", T)
    # ---------- incident / SEC / PIR ----------
    timeline=[("2026-07-14T22:47:13+05:30","Citrix SSO from unregistered personal laptop over home Wi-Fi (NW1109) to fix failed pipeline ldp-accrual-nightly","OKTA-EVT-20260714-2247"),
      ("2026-07-30T15:10:00+05:30","Detected: Ashgrove auditor sample of Citrix logs flags unregistered device","AUD-2026-01"),
      ("2026-08-01T09:15:00+05:30","SEC-2026-0077 opened by SOC (Sev2: policy violation with potential exposure); IT Security Manager notified","SEC-2026-0077"),
      ("2026-08-01T10:30:00+05:30","Upgraded to Sev1: session confirmed to have queried BM-LOYALTY-PROD members table (PII). Awareness clock T0 for DPA §8","SEC-2026-0077"),
      ("2026-08-01T10:40:00+05:30","DPO (Grace Okafor) and DM (Siddharth Chauhan) notified per POL-SEC-IR §4","CHT-2026-031"),
      ("2026-08-02T12:30:00+05:30","Brightmart DPO (Hannah Price) notified by Northwind DPO: T0+26h; DPA §8 requires 24h - LATE. Team used IR §4 72h default","CHT-2026-031"),
      ("2026-08-03T11:05:00+05:30","Containment: NW1109 Citrix access suspended","OKTA-EVT-20260803-SUSP"),
      ("2026-08-06T16:00:00+01:00","Brightmart formal escalation ESC-2026-005 (audit right §16; remediation plan in 14 days)","ESC-2026-005"),
      ("2026-08-12T15:00:00+05:30","Post-incident review PIR-2026-014 completed; CAPA-1..4 raised","PIR-2026-014"),
      ("2026-08-18T17:00:00+01:00","Remediation plan sent to Brightmart","DEC-2026-036"),
      ("2026-09-01T10:20:00+05:30","Access restored on enrolled device after re-training","ITT-2026-2301"),
      ("2026-09-15T18:00:00+05:30","CAPA-1 due date passed at 80% - overdue","ITT-2026-2210")]
    inc={**meta("ServiceNow SecOps","INC-2026-014",ts("2026-08-01","09:15:00"),SB,updated_at=ts("2026-09-22"),
        restricted_fields=[rf("subject_person.name","SECURITY","External auditors and non-account roles see employee ID NW1109 only"),
                           rf("hr_followup","RESTRICTED-HR","Existence only: 'HR follow-up exists'")]),
        "title":"Brightmart client PII accessed from unmanaged personal device","severity_initial":"Sev2","severity":"Sev1","account":"ACC-BRM",
        "sec_ticket":"SEC-2026-0077","subject_person":{"employee_id":"NW1109","name":"Imran Sheikh"},
        "data_involved":"BM-LOYALTY-PROD members table (loyalty member name, email, postcode, purchase history) - viewed in session; no download evidenced",
        "detected_by":"EXT-AA-01 (audit sample)","awareness_t0":"2026-08-01T10:30:00+05:30",
        "client_notification":{"required_by":"ADD-BRM-GDPR-01 §8 / MSA-BRM-2026 §12 (24h)","deadline":"2026-08-02T10:30:00+05:30",
                               "sent_at":"2026-08-02T12:30:00+05:30","elapsed_hours":26,"met":False,"sent_by":"NW0402","to":"EXT-BM-03",
                               "cause":"Team applied POL-SEC-IR v2 §4 default 72h instead of contractual 24h"},
        "regulator_notification":{"assessment":"Controller (Brightmart) decision; Northwind as processor notified controller","by":None},
        "timeline":[{"at":a,"event":b,"evidence":c} for a,b,c in timeline],
        "hr_followup":"HRC-2026-0031","pir":"PIR-2026-014","escalation":"ESC-2026-005","findings":["AUD-2026-01"],"status":"Closed - CAPA open"}
    sec={**meta("ServiceNow SecOps","SEC-2026-0077",ts("2026-08-01","09:15:00"),SB,updated_at=ts("2026-09-01")),"opened_by":"NW0503","assigned_to":"NW0501",
        "severity_history":[{"at":ts("2026-08-01","09:15:00"),"sev":"Sev2"},{"at":ts("2026-08-01","10:30:00"),"sev":"Sev1"}],
        "incident":"INC-2026-014","actions":["2026-08-03 access suspended (NW1109)","2026-09-01 access restored (ITT-2026-2301)"],"status":"Resolved"}
    pir={**meta("ServiceNow SecOps","PIR-2026-014",ts("2026-08-12","15:00:00"),SB),"incident":"INC-2026-014","participants":["NW0501","NW1701","NW0502","NW0503","NW1101","NW0402"],
        "due_by":"2026-08-14 (10 working days per POL-SEC-IR §6)","completed":"2026-08-12",
        "root_cause":"Okta device assurance / MDM conditional access was not enforced on the Brightmart Citrix gateway, so an unregistered personal laptop could authenticate. Contributing: no on-call laptop for late-night pipeline support; June access review skipped; IR runbook lacked client-specific notification clocks.",
        "capa":[{"id":"CAPA-2026-014-1","action":"Enforce device compliance on Citrix gateway","owner":"NW1701","due":"2026-09-15","status":"Open - overdue (80%)","ticket":"ITT-2026-2210"},
                {"id":"CAPA-2026-014-2","action":"Client notification-clock lookup in IR runbook","owner":"NW0501","due":"2026-08-31","status":"Closed","ticket":"ITT-2026-2215"},
                {"id":"CAPA-2026-014-3","action":"Access-review calendar with mandatory delegate","owner":"NW1103","due":"2026-09-30","status":"Open","ticket":"ITT-2026-2216"},
                {"id":"CAPA-2026-014-4","action":"Complete A.6.3 training for 3 staff","owner":"NW1117","due":"2026-08-31","status":"Closed","ticket":"ITT-2026-2217"}]}
    write_json("security_grc/incidents.json",[inc,sec,pir])
    # ---------- audit findings ----------
    AUD=[{**meta("Vanta (Audit)","AUD-2026-01",ts("2026-07-30","17:00:00"),SEC,updated_at=ts("2026-09-22","11:00:00")),"audit":"ISO 27001:2022 surveillance audit 1 (Ashgrove Assurance) 2026-07-28..31",
          "auditor":"EXT-AA-01","grade":"Major nonconformity","controls":["A.8.1","A.6.7"],
          "statement":"Brightmart Citrix login on 2026-07-14 from a device not registered in the endpoint inventory (user NW1109). Device controls in POL-SEC-CDH §2 and WFH v3 §2.4 not enforced technically.",
          "evidence":["OKTA-EVT-20260714-2247","OKTA-EVT-20260714-2249","DEV inventory export 2026-07-30"],"capa":"CAPA-2026-014-1",
          "updates":[{"at":ts("2026-09-22","11:00:00"),"by":"EXT-AA-01","note":"Requests CAPA-1 closure evidence by 2026-10-30"}],"evidence_due":"2026-10-30","status":"Open"},
         {**meta("Vanta (Audit)","AUD-2026-02",ts("2026-07-31","12:00:00"),SEC),"audit":"ISO 27001 surveillance 2026","auditor":"EXT-AA-01","grade":"Minor nonconformity","controls":["A.5.18"],
          "statement":"Monthly Brightmart access review not performed for June 2026 (no certification record; delegation DLG-2026-004 expired 2026-02-28).",
          "evidence":["APR-2026-0529","APR-2026-0729","DLG-2026-004","(absence) no June 2026 record"],"capa":"CAPA-2026-014-3","status":"Open - catch-up done APR-2026-0908"},
         {**meta("Vanta (Audit)","AUD-2026-03",ts("2026-07-31","12:30:00"),SEC),"audit":"ISO 27001 surveillance 2026","auditor":"EXT-AA-01","grade":"Observation","controls":["A.6.3"],
          "statement":"Annual security awareness training incomplete for 3 Brightmart staff (NW1109, NW1113, NW1116).","evidence":["TRN records"],"capa":"CAPA-2026-014-4","status":"Closed 2026-08-29"}]
    write_json("security_grc/audit_findings.json",AUD)
    # ---------- evidence items ----------
    E=[("EVD-001","A.5.18","Monthly access certification exports Mar-May, Jul, Aug (June missing)",["APR-2026-0330","APR-2026-0429","APR-2026-0529","APR-2026-0729","APR-2026-0908"]),
       ("EVD-002","A.8.1","Intune compliant-device inventory for Brightmart team",["DEV-NW-LT-1109"]),
       ("EVD-003","A.6.3","Training completion report 2026",["TRN-2026-BRM"]),
       ("EVD-004","A.5.24","POL-SEC-IR v2 + IR tabletop record 2026-03",["POL-SEC-IR-v2"]),
       ("EVD-005","A.5.26","INC-2026-014 + PIR-2026-014",["INC-2026-014","PIR-2026-014"]),
       ("EVD-006","A.5.34","Brightmart DPA + RoPA entry",["ADD-BRM-GDPR-01"]),
       ("EVD-007","A.6.7","WFH v3 + DM override for Brightmart",["POL-HR-WFH-v3","DEC-2026-014"]),
       ("EVD-008","A.8.16","SIEM alert rules export",[]),("EVD-009","A.8.12","DLP policy export (no download events for INC-2026-014)",["INC-2026-014"]),
       ("EVD-010","A.5.15","Access control standard",["POL-SEC-CDH-v2"]),("EVD-011","A.8.1","CAPA-1 progress (80%)",["ITT-2026-2210"]),
       ("EVD-012","A.5.1","ISMS SoA 2026.1",["ISMS-SOA-2026.1"])]
    write_json("security_grc/evidence.json",[{**meta("Vanta (Evidence)",i,ts("2026-07-27"),SEC),"control":f"CTL-{c}","description":d,"artifacts":a,
        "collected_by":"NW0502","status":"Missing period: 2026-06" if i=="EVD-001" else "Accepted"} for i,c,d,a in E])
    # ---------- access review schedule (expected vs actual) ----------
    sched=[]
    for m,rec in [("2026-03","APR-2026-0330"),("2026-04","APR-2026-0429"),("2026-05","APR-2026-0529"),("2026-06",None),("2026-07","APR-2026-0729"),("2026-08","APR-2026-0908")]:
        sched.append({**meta("Vanta (Access Reviews)",f"ARS-BRM-{m}",ts(m+"-01"),SB),"control":"CTL-A.5.18","account":"ACC-BRM","period":m,
            "expected_reviewer":"NW1102" if m<"2026-08" else "NW1103","completed_record":rec,"status":"Completed" if rec else "MISSED (no record)",
            **({"note":"Ritu busy with architect handover; no valid delegation (DLG-2026-004 expired 2026-02-28). Caught up in APR-2026-0908."} if not rec else {})})
    write_json("security_grc/access_review_schedule.json",sched)
    # ---------- training ----------
    TR=[]
    for p in ["NW1101","NW1102","NW1103","NW1105"]+BRM_TEAM:
        incomplete = p in ("NW1109","NW1113","NW1116")
        TR.append({**meta("KnowBe4 LMS",f"TRN-2026-{p}",ts("2026-01-10"),SEC,acl=CLASS_ACL[SEC]+[p,"grp-acc-brm-dm"],updated_at=ts("2026-08-29")),
           "employee_id":p,"course":"Security Awareness & Client Data Handling 2026","assigned":"2026-01-10","due":"2026-06-30",
           "completed": "2026-08-29" if incomplete else f"2026-0{r.randint(3,6)}-{r.randint(1,28):02d}",
           "status_at_audit":"Incomplete" if incomplete else "Complete"})
    TR.append({**meta("KnowBe4 LMS","TRN-2026-NW1109-RT",ts("2026-08-20"),"RESTRICTED-HR",acl=CLASS_ACL["RESTRICTED-HR"]+["grp-security"]),"employee_id":"NW1109",
               "course":"Remedial: Client Data Handling & Device Policy","completed":"2026-08-29","linked":"HRC-2026-0031"})
    write_json("security_grc/training_records.json",TR)
    return {"controls":len(controls),"okta_events":len(ev),"tickets":len(T),"training":len(TR)}
