# DATA_CATALOG — Northwind synthetic source data

Regenerate structured data: `cd generator && python3 run_all.py && python3 check_questions.py && python3 catalog.py` (seed `NORTHWIND_SEED=2026`). Markdown documents in policies/, contracts/, decisions/, collab/minutes/ and hris/ORG-2026-05.md are hand-composed and not overwritten.

Every record carries `id`, `source_system`, `created_at`/`updated_at`, `classification`, `acl` (groups/users) — at top level, in `x_northwind` (SCIM/Okta), or in YAML front-matter (markdown). Field-level restrictions are in `restricted_fields[]` (`field`, `classification`, `acl`, `existence_disclosure`). Classification→ACL map: `directory/acl_classifications.json`.


## directory/ — Okta / Entra ID
SCIM 2.0 Users (core + enterprise extension + Northwind HR ext) & Groups; ACL classification map. Implicitly used by every question (identity, grade, manager, group-based ACL evaluation)

| File | Records | Format |
|---|---|---|
| `directory/acl_classifications.json` | 1 | json |
| `directory/scim_groups.json` | 22 | json |
| `directory/scim_users.json` | 3002 | json |

**Questions served:** Q09


## hris/ — Workday
Effective-dated job events (JSON+CSV), worker snapshot, skills/certs, restricted HR case stubs, ORG-2026-05 memo

| File | Records | Format |
|---|---|---|
| `hris/ORG-2026-05.md` | 1 | md |
| `hris/hr_cases.json` | 16 | json |
| `hris/job_events.csv` | 3948 | csv |
| `hris/job_events.json` | 3948 | json |
| `hris/skills_certs.json` | 55 | json |
| `hris/workers.csv` | 3000 | csv |

**Questions served:** Q03, Q07, Q08, Q22, Q26, Q28, Q38


## policies/ — SharePoint / Confluence
Markdown + YAML front-matter (doc_id, version, status, effective dates, supersedes, owner, approver, scope, classification, acl); DoA matrix CSV

| File | Records | Format |
|---|---|---|
| `policies/DRAFT-POL-HR-WFH-v3-rc1.md` | 1 | md |
| `policies/ISMS-SOA-2026.1.md` | 1 | md |
| `policies/POL-FIN-AUTH-v5.md` | 1 | md |
| `policies/POL-FIN-PRICE-v3.md` | 1 | md |
| `policies/POL-HR-LEAVE-v4.md` | 1 | md |
| `policies/POL-HR-TRAVEL-v3.md` | 1 | md |
| `policies/POL-HR-WFH-v2.md` | 1 | md |
| `policies/POL-HR-WFH-v3.md` | 1 | md |
| `policies/POL-RMG-BENCH-v2.md` | 1 | md |
| `policies/POL-RMG-BENCH-v3.md` | 1 | md |
| `policies/POL-SEC-CDH-v2.md` | 1 | md |
| `policies/POL-SEC-IR-v2.md` | 1 | md |
| `policies/delegation_of_authority_matrix.csv` | 30 | csv |

**Questions served:** Q01, Q02, Q11, Q12, Q13, Q14, Q15, Q16, Q18, Q19, Q26, Q27, Q28, Q29, Q30, Q34, Q36


## contracts/ — Icertis CLM
MSA/SOW/DPA markdown with clause numbering; obligations.json; client escalations

| File | Records | Format |
|---|---|---|
| `contracts/ADD-BRM-GDPR-01.md` | 1 | md |
| `contracts/MSA-BRM-2026.md` | 1 | md |
| `contracts/MSA-CIN-2024.md` | 1 | md |
| `contracts/MSA-HBK-2023.md` | 1 | md |
| `contracts/MSA-MED-2025.md` | 1 | md |
| `contracts/MSA-VER-2025.md` | 1 | md |
| `contracts/MSA-ZEN-2022.md` | 1 | md |
| `contracts/SOW-BRM-01.md` | 1 | md |
| `contracts/SOW-CIN-03.md` | 1 | md |
| `contracts/escalations.json` | 2 | json |
| `contracts/obligations.json` | 13 | json |

**Questions served:** Q04, Q10, Q11, Q14, Q15, Q18, Q19, Q20, Q21, Q23, Q24, Q35, Q36


## approvals/ — SAP Concur / Salesforce Deal Desk / Coupa / ServiceNow / Workday Absence
Approval records with steps[] (approver, action, ts, comment, delegation); pricing-exception register; background noise JSONL

| File | Records | Format |
|---|---|---|
| `approvals/approval_records.json` | 37 | json |
| `approvals/background_approvals.jsonl` | 2500 | jsonl |
| `approvals/pricing_exceptions.json` | 8 | json |

**Questions served:** Q01, Q02, Q04, Q05, Q10, Q17, Q19, Q21, Q23, Q26, Q27, Q28, Q29, Q30, Q31, Q35, Q37, Q39


## decisions/ — Confluence decision log
Nygard-style ADR/RFC markdown (status, supersedes/superseded_by, governing policy, evidence)

| File | Records | Format |
|---|---|---|
| `decisions/DEC-2026-001.md` | 1 | md |
| `decisions/DEC-2026-005.md` | 1 | md |
| `decisions/DEC-2026-006.md` | 1 | md |
| `decisions/DEC-2026-008.md` | 1 | md |
| `decisions/DEC-2026-011.md` | 1 | md |
| `decisions/DEC-2026-013.md` | 1 | md |
| `decisions/DEC-2026-014.md` | 1 | md |
| `decisions/DEC-2026-015.md` | 1 | md |
| `decisions/DEC-2026-017.md` | 1 | md |
| `decisions/DEC-2026-032.md` | 1 | md |
| `decisions/DEC-2026-034.md` | 1 | md |
| `decisions/DEC-2026-036.md` | 1 | md |
| `decisions/RFC-2026-002.md` | 1 | md |
| `decisions/RFC-2026-010.md` | 1 | md |

**Questions served:** Q11, Q12, Q13, Q14, Q17, Q18, Q19, Q20, Q21, Q22, Q24, Q38


## engineering/ — GitHub Enterprise / Jira Cloud
Repos, CODEOWNERS files + history, PRs with reviews, commits, runbook blame, Jira issues incl. RAID, activity event stream

| File | Records | Format |
|---|---|---|
| `engineering/activity_events.jsonl` | 418 | jsonl |
| `engineering/codeowners/infra-brightmart.CODEOWNERS` | 1 | CODEOWNERS |
| `engineering/codeowners/loyalty-ingest-pipeline.CODEOWNERS` | 1 | CODEOWNERS |
| `engineering/codeowners/loyalty-service.CODEOWNERS` | 1 | CODEOWNERS |
| `engineering/codeowners/member-portal-web.CODEOWNERS` | 1 | CODEOWNERS |
| `engineering/codeowners_history.json` | 2 | json |
| `engineering/commits.json` | 353 | json |
| `engineering/file_history_runbook.json` | 1 | json |
| `engineering/jira_issues.json` | 132 | json |
| `engineering/pull_requests.json` | 142 | json |
| `engineering/repositories.json` | 4 | json |

**Questions served:** Q22, Q23, Q25, Q36


## collab/ — Slack / Teams / M365 Calendar
Slack-export-style threads, meeting minutes (decisions + actions only), calendar metadata

| File | Records | Format |
|---|---|---|
| `collab/calendar_events.json` | 10 | json |
| `collab/minutes/MOM-2026-001.md` | 1 | md |
| `collab/minutes/MOM-2026-004.md` | 1 | md |
| `collab/minutes/MOM-2026-007.md` | 1 | md |
| `collab/minutes/MOM-2026-009.md` | 1 | md |
| `collab/minutes/MOM-2026-012.md` | 1 | md |
| `collab/minutes/MOM-2026-015.md` | 1 | md |
| `collab/minutes/MOM-2026-018.md` | 1 | md |
| `collab/minutes/MOM-2026-024.md` | 1 | md |
| `collab/minutes/MOM-2026-026.md` | 1 | md |
| `collab/minutes/MOM-2026-030.md` | 1 | md |
| `collab/minutes/MOM-2026-035.md` | 1 | md |
| `collab/minutes/MOM-2026-038.md` | 1 | md |
| `collab/slack_threads.json` | 12 | json |

**Questions served:** Q02, Q11, Q14, Q17, Q22, Q23, Q25, Q37, Q39


## staffing/ — Kantata PSA
Allocations (hard/soft, %, dates), demand & bench-pull records, milestones

| File | Records | Format |
|---|---|---|
| `staffing/allocations.json` | 72 | json |
| `staffing/demands_and_bench_pulls.json` | 2 | json |
| `staffing/milestones.json` | 6 | json |

**Questions served:** Q20, Q21, Q22, Q34, Q35, Q36


## security_grc/ — Vanta / Okta System Log / ServiceNow SecOps / Intune / KnowBe4
ISO 27001:2022 controls, evidence, Okta System Log events, incident+SEC+PIR, audit findings, access-review schedule, ITSM tickets, devices, training

| File | Records | Format |
|---|---|---|
| `security_grc/access_review_schedule.json` | 6 | json |
| `security_grc/audit_findings.json` | 3 | json |
| `security_grc/controls.json` | 15 | json |
| `security_grc/evidence.json` | 12 | json |
| `security_grc/incidents.json` | 3 | json |
| `security_grc/intune_devices.json` | 15 | json |
| `security_grc/itsm_tickets.json` | 137 | json |
| `security_grc/okta_system_log.json` | 102 | json |
| `security_grc/training_records.json` | 17 | json |

**Questions served:** Q03, Q06, Q07, Q08, Q14, Q16, Q20, Q24, Q25, Q31, Q32, Q33, Q34, Q37


**Total records/documents:** 18098
