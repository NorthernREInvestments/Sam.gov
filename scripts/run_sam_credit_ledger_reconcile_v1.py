"""Local / offline SAM credit ledger reconcile — zero live SAM API calls."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from discovery.sam_budgeted_client import (  # noqa: E402
    BUILD,
    owner_credit_dashboard,
    reconcile_sam_credit_ledger,
)

OUT = ROOT / "data" / "m3_sam_api_credit_ledger_reconciliation_v1_report.json"


def main() -> int:
    recon = reconcile_sam_credit_ledger(persist=True)
    owner = owner_credit_dashboard()
    report = {
        "kind": "SamApiCreditLedgerReconciliationV1",
        "build": BUILD,
        "reconcile": recon,
        "owner": owner,
        "REPORT": {
            "REAL_SAM_API_CALLS_TODAY": owner.get("REAL_SAM_API_CALLS_TODAY"),
            "CACHE_HITS": owner.get("CACHE_HITS"),
            "PUBLIC_NOTICEDESC_FETCHES": owner.get("PUBLIC_NOTICEDESC_FETCHES"),
            "AGENCY_URL_FETCHES": owner.get("AGENCY_URL_FETCHES"),
            "INTERNAL_ATTEMPTS": owner.get("INTERNAL_ATTEMPTS"),
            "RETRIES": owner.get("RETRIES"),
            "BLOCKED_OVER_CAP_ATTEMPTS": owner.get("BLOCKED_OVER_CAP_ATTEMPTS"),
            "REMAINING_REAL_CREDITS": owner.get("REMAINING_REAL_CREDITS"),
        },
        "GATES": owner.get("GATES"),
        "WHY_PRIOR_SHOWED_13": owner.get("WHY_PRIOR_INFLATED")
        or recon.get("WHY_PRIOR_SHOWED_INFLATED"),
        "NO_NEW_SAM_CREDITS": True,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["REPORT"], indent=2))
    print("GATES:", json.dumps(report["GATES"], indent=2))
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
