---
doc_id: POL-FIN-AUTH
title: "Delegation of Authority & Expense Approval Matrix"
version: "v5"
status: approved
effective_from: 2025-10-01
effective_to: null
supersedes: POL-FIN-AUTH@v4
superseded_by: null
owner: NW0015 (Neha Joshi)
approver: NW0002 (Anjali Kapoor)
scope: {country: [IN, GB], unit: all, grade: all}
classification: PUBLIC-INT
acl: [grp-employee]  # exception log: MGR; amounts RESTRICTED-FIN
source_system: SharePoint (Finance)
id: POL-FIN-AUTH@v5
created_at: 2025-09-20T10:00:00+05:30
updated_at: 2025-09-20T10:00:00+05:30
---
# Delegation of Authority & Expense Approval Matrix (v5)

Machine-readable matrix: `delegation_of_authority_matrix.csv` (rows DOA-01..DOA-30). "Approver" = lowest grade that may approve **alone**.

| Item | ≤ limit A | Approver | ≤ limit B | Approver | Above B |
|---|---|---|---|---|---|
| Business expense (per claim) | ₹25,000 | Line manager (G4+) | ₹2,00,000 | G6+ in reporting line | G8 Unit Head + Financial Controller |
| Domestic travel | ₹50,000/trip | Line manager | ₹1,50,000 | G6 | G8 |
| International travel | — | Always G6 + Unit Head (G8) | client-billable: + DM confirms billability | | > 30 days: CHRO (immigration) |
| Leave | ≤ 5 days | Line manager | 6–15 days | + PM if on billable project | > 15 / unpaid: HRBP + G6 |
| Client discount vs rate card | ≤ 10% | DM (G6) | 10–15% | Unit Head (G8) | > 15%: CFO **and** CEO + Pricing Committee note |
| Bench pull | any | RM Manager (G5) | | | |
| Pull from active billable project | — | Releasing DM **and** receiving DM | < 2 weeks' notice | + Unit Head of releasing unit | key-person clause: + client consent (Legal) |
| Hotfix / unbudgeted cloud spend | ₹1,00,000 | DM | ₹10,00,000 | Unit Head | CFO |

**5.1 UK entity.** Northwind Consulting UK Ltd expense limits are GBP: A = £250, B = £2,000.

**5.2 Reporting line.** Approval follows the requester's reporting line **as of the request date**.

**5.3 Delegation.** An approver on leave may delegate to a peer of the **same or higher** grade (access-approval delegations may name the project's tech lead). Delegations must be recorded in the DLG register with start and end dates and lapse automatically.

**5.4 Exceptions** are logged in the Exception Register (MGR access; amounts RESTRICTED-FIN).

**5.5 Self-approval** is never allowed; if the approver is the requester, the request goes up one level.
