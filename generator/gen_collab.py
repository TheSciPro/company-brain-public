"""collab/ Slack-export-like threads (hand-composed text) + calendar event metadata (Google/Outlook-like)."""
from common import *
B="INTERNAL-ACCOUNT-BRM"
def M(pid, t, text): return {"user":pid,"user_name":NAME[pid],"ts":t,"text":text}
THREADS=[
 ("CHT-2026-003","#brightmart-bid","RESTRICTED-FIN-DM",["DEC-2026-005","DQ-BRM-2026-001-v1"],[
   M("NW1118","2026-02-03T14:05:00+00:00","Call with Mark Lewis (Brightmart VM) done. They have a competing bid roughly 20% under our 12% draft number. He was explicit: 12% won't get through their procurement board."),
   M("NW1101","2026-02-03T19:50:00+05:30","Then 12% is dead. Manoj, can we model 18% with a renewal reprice? Anything >15% goes to CFO+CEO."),
   M("NW0202","2026-02-03T20:15:00+05:30","Modelled: 18% gives ~21% floor margin vs 28% target. Needs Pricing Committee note. Booking committee for Thu 5 Feb."),
   M("NW0011","2026-02-03T20:40:00+05:30","Agree. Strategic UK logo; I'll recommend at committee.")]),
 ("CHT-2026-005","#brightmart-legal","CLIENT-CONF-BRM",["DEC-2026-006","ADD-BRM-GDPR-01"],[
   M("NW0401","2026-02-11T11:00:00+05:30","Brightmart DPO (Hannah Price) wants breach notice in 24h, not 72h, and no remote working for anyone touching member PII."),
   M("NW0402","2026-02-11T07:30:00+00:00","24h to the controller is stricter than our IR default (72h) but acceptable. We must make sure the IR runbook carries the client clock."),
   M("NW0401","2026-02-11T12:10:00+05:30","Accepting both: DPA §8 (24h) and §11 / MSA §7.3 (office-only from ODC-2). Flagging to Siddharth for team planning.")]),
 ("CHT-2026-007","#rmg-staffing","CLIENT-CONF-CIN",["APR-2026-0219","DEC-2026-008","STF-2026-0146"],[
   M("NW1202","2026-02-18T10:20:00+05:30","Varun is a named key person in SOW-CIN-03 Annex B and M3 is due 30 Apr. Pulling him now is a real risk."),
   M("NW0801","2026-02-18T10:45:00+05:30","Brightmart needs Guidewire-free Java/Kafka depth by 23 Feb; Varun is the only fit not on bench. Bench v2 only needs your consent as releasing DM."),
   M("NW1201","2026-02-18T11:05:00+05:30","Policy says 10 working days handover. We'd get 3. No backfill named."),
   M("NW1202","2026-02-19T17:00:00+05:30","Approving under protest with 3 days handover. Logging the M3 risk. @Prakash please start backfill search.")]),
 ("CHT-2026-010","#brightmart-eng","INTERNAL-ACCOUNT-BRM",["RFC-2026-010","DEC-2026-011"],[
   M("NW1110","2026-04-14T15:00:00+05:30","Consumer lag spiking on points-accrual after rebalance - who knows the offset commit logic?"),
   M("NW1106","2026-04-14T15:04:00+05:30","That's the manual-commit path I wrote for idempotency. Don't reset offsets; replay from DLQ with the runbook script. Pushing a fix to the rebalance listener."),
   M("NW1105","2026-04-14T15:20:00+05:30","Thanks Deepa. Keep me as reviewer for visibility.")]),
 ("CHT-2026-016","#brightmart-team","PUBLIC-INT",["DEC-2026-014","POL-HR-WFH-v3"],[
   M("NW1108","2026-04-02T10:02:00+05:30","WFH v3 is live from yesterday - does that mean we get 3 days WFH now?"),
   M("NW1110","2026-04-02T10:05:00+05:30","Also the wiki page said 4 days?"),
   M("NW0301","2026-04-02T11:30:00+05:30","The 4-day page was a draft (rc1), never effective. v3 gives 3 days generally, BUT v3 §2.5 says client contracts prevail. Brightmart contract is office-only - Siddharth will confirm."),
   M("NW1101","2026-04-02T12:10:00+05:30","Confirming: Brightmart team stays office-only at ODC-2 (MSA §7.3, DPA §11). Formal note going out tomorrow (DEC-2026-014).")]),
 ("CHT-2026-019","#coastline-delivery","CLIENT-CONF-CIN",["ESC-2026-002","APR-2026-0506"],[
   M("NW1201","2026-04-30T18:00:00+05:30","M3 will slip ~3 weeks. Claims-module knowledge left with Varun; backfill only ramped 2 weeks ago."),
   M("NW1202","2026-04-30T18:30:00+05:30","Daniel Reyes has escalated formally. Expect a service-credit claim under §8."),
   M("NW0010","2026-04-30T19:05:00+05:30","I was never consulted on this release. We need a policy fix - raising with Sanjay.")]),
 ("CHT-2026-022","#brightmart-leadership","CLIENT-CONF-BRM",["APR-2026-0610","STF-2026-0388"],[
   M("NW1104","2026-05-20T16:00:00+05:30","Informed Sarah Mitchell today: I'm moving to HarbourBank from 15 Jun. Proposing Mohit as acting architect; Deepa owns most of loyalty-service day to day."),
   M("NW1118","2026-05-20T12:30:00+00:00","Sarah is unhappy - MSA §9 needs 30 days' notice and her approval. We're short. I'll push for approval."),
   M("NW1101","2026-05-20T17:10:00+05:30","Raising client approval request (APR-2026-0610). Ananya, please plan a proper handover with Deepa and Mohit.")]),
 ("CHT-2026-028","#brightmart-oncall","SECURITY-BRM",["BRM-377","INC-2026-014"],[
   M("NW1109","2026-07-14T22:31:00+05:30","ldp-accrual-nightly failed again (executor OOM). My office laptop is at ODC, logging in from home to fix."),
   M("NW1109","2026-07-15T01:36:00+05:30","Fixed, job rerun. Points accrued for 14 Jul.")]),
 ("CHT-2026-031","#inc-2026-014","SECURITY-BRM",["INC-2026-014","DEC-2026-032","ADD-BRM-GDPR-01"],[
   M("NW0501","2026-08-01T10:35:00+05:30","Sev1 confirmed 10:30: session queried BM-LOYALTY-PROD members table. DPO + DM notified. Client notice per IR §4 default 72h."),
   M("NW1101","2026-08-01T10:50:00+05:30","Ack. Prepping client comms."),
   M("NW0402","2026-08-02T07:50:00+01:00","Correction: Brightmart DPA §8 is 24h from awareness, not 72h. Sending to Hannah Price now - we are late."),
   M("NW0402","2026-08-02T08:00:00+01:00","Sent to Brightmart DPO at 08:00 UK (12:30 IST), 26h after awareness. Logging the miss (DEC-2026-032).")]),
 ("CHT-2026-033","#brightmart-leadership","CLIENT-CONF-BRM",["ESC-2026-005","DEC-2026-034"],[
   M("NW1118","2026-08-06T15:30:00+00:00","Mark Lewis has invoked the audit right (§16) and wants a remediation plan in 14 days, by 20 Aug."),
   M("NW0013","2026-08-06T15:45:00+00:00","Exec call on 10 Aug. We need a fresh PM and a dedicated security champion.")]),
 ("CHT-2026-040","#brightmart-team","INTERNAL-ACCOUNT-BRM",["MOM-2026-038","ITT-2026-2301"],[
   M("NW1103","2026-08-26T18:00:00+05:30","Hi all, I'm the new PM from today. Handover with Ritu done - 7 open actions; I'll own the monthly access review."),
   M("NW1106","2026-08-26T18:10:00+05:30","Welcome Nikhil! Runbook for loyalty-service is in docs/runbook.md - happy to walk you through.")]),
 ("CHT-2026-044","#brightmart-leadership","CLIENT-CONF-BRM",["AUD-2026-01","ITT-2026-2210"],[
   M("NW0502","2026-09-22T12:00:00+05:30","Ashgrove (Claire Bennett) wants CAPA-1 closure evidence by 30 Oct. Rakesh says 80% and the last 2 nodes are blocked by Brightmart's change freeze."),
   M("NW1103","2026-09-22T12:20:00+05:30","Raising dependency BRM-RAID-D01 with Brightmart Citrix team; target 9 Oct.")]),
]
CAL=[("CAL-2026-0205-PC","Pricing Committee: Brightmart","2026-02-05","16:00","17:30",["NW0002","NW0202","NW0011","NW0015"],"MOM-2026-007","RESTRICTED-FIN-DM"),
     ("CAL-2026-0216-RM","Brightmart staffing plan","2026-02-16","11:00","12:00",["NW0801","NW0802","NW1101","NW1102"],"MOM-2026-009",B),
     ("CAL-2026-0302-KO","Brightmart kick-off","2026-03-02","10:00","12:00",["NW1101","NW1102","NW1118","NW1104","NW1105"]+BRM_TEAM,"MOM-2026-012",B),
     ("CAL-2026-0309-ARB","Architecture review: loyalty-service","2026-03-09","15:00","16:30",["NW1104","NW1106","NW1105","NW1112"],"RFC-2026-010",B),
     ("CAL-2026-0408-EX","Exec: Consumer unit design","2026-04-08","14:00","15:00",["NW0001","NW0003","NW0004"],"MOM-2026-018","MGR"),
     ("CAL-2026-0612-HO","Architecture handover Ananya -> Mohit/Deepa","2026-06-12","14:00","17:00",["NW1104","NW1105","NW1106","NW1102"],"MOM-2026-024",B),
     ("CAL-2026-0728-AUD","ISO 27001 surveillance audit opening","2026-07-28","09:30","10:30",["EXT-AA-01","EXT-AA-02","NW0502","NW0006","NW0501"],"MOM-2026-030","SECURITY"),
     ("CAL-2026-0810-EX","Brightmart escalation response","2026-08-10","15:00","16:00",["NW0013","NW1101","NW0006","NW1118"],"MOM-2026-035","CLIENT-CONF-BRM"),
     ("CAL-2026-0826-HO","PM handover Ritu -> Nikhil","2026-08-26","11:00","13:00",["NW1102","NW1103","NW1101"],"MOM-2026-038",B),
     ("CAL-2026-1005-SC","Brightmart steering committee (London)","2026-10-07","10:00","12:00",["NW1103","NW1101","NW1118","EXT-BM-01","EXT-BM-02"],None,"CLIENT-CONF-BRM")]
def run():
    out=[]
    for id_,ch,cls,links,msgs in THREADS:
        out.append({**meta("Slack",id_,msgs[0]["ts"],cls,updated_at=msgs[-1]["ts"]),"channel":ch,"thread_ts":msgs[0]["ts"],
                    "participants":sorted({m["user"] for m in msgs}),"links":links,"messages":msgs})
    write_json("collab/slack_threads.json",out)
    write_json("collab/calendar_events.json",[{**meta("Microsoft 365 Calendar",i,ts(d,a),c),"subject":s,"start":ts(d,a+":00"),"end":ts(d,b+":00"),
        "attendees":[{"id":p,"name":NAME[p]} for p in att],"minutes":m} for i,s,d,a,b,att,m,c in CAL])
    return {"threads":len(out),"calendar":len(CAL)}
