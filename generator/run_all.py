"""Re-runnable deterministic generator. Usage: python3 run_all.py  (NORTHWIND_SEED=2026 default).
Writes structured data under ../data/. Hand-composed markdown (policies, contracts, decisions, minutes, ORG memo) is NOT overwritten."""
import json
import gen_directory_hris, gen_approvals_staffing, gen_security, gen_engineering, gen_collab, gen_contracts_policies, render_questions
from common import SEED
if __name__ == "__main__":
    out={"seed":SEED}
    for m in [gen_directory_hris, gen_approvals_staffing, gen_security, gen_engineering, gen_collab, gen_contracts_policies, render_questions]:
        out[m.__name__]=m.run()
    print(json.dumps(out, indent=1))
