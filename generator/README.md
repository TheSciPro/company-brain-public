# Northwind data generator
Deterministic (seed `NORTHWIND_SEED`, default 2026). From this folder:
```
python3 run_all.py          # regenerate all structured data under ../data
python3 check_questions.py  # validate ../demo_questions.json evidence -> ../validation_report.txt (exit 1 on gaps)
python3 catalog.py          # rebuild ../DATA_CATALOG.md
```
Modules: common.py (cast, ACL map, envelope helpers), people.py (2,945 background employees), gen_directory_hris.py, gen_approvals_staffing.py, gen_security.py, gen_engineering.py, gen_collab.py, gen_contracts_policies.py, questions.py (question bank source of truth) -> render_questions.py.
Hand-composed markdown (policies/, contracts/, decisions/, collab/minutes/, hris/ORG-2026-05.md) is never overwritten.
