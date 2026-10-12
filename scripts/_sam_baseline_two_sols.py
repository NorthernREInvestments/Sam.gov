"""Two-solicitation SAM baseline lookup (max 2 calls)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from buyer_intelligence.commercial_budget_control_v1 import check_budget, dashboard_payload
from buyer_intelligence.commercial_intelligence_upgrade_v1 import _save, _utc


def _record_sam(n: int = 1, **meta) -> None:
    try:
        from buyer_intelligence.commercial_budget_control_v1 import record_usage

        record_usage(provider="sam", sam_calls=n, meta=meta)
    except Exception:
        try:
            from buyer_intelligence.commercial_budget_control_v1 import consume_sam

            consume_sam(n)
        except Exception:
            pass


def search(sol: str, api_key: str) -> dict:
    url = "https://api.sam.gov/opportunities/v2/search"
    params = {
        "api_key": api_key,
        "solicitationNumber": sol,
        "limit": 5,
        "postedFrom": "01/01/2025",
        "postedTo": "12/31/2027",
    }
    r = httpx.get(url, params=params, timeout=30.0)
    _record_sam(1, purpose="baseline_solicitation_lookup", sol=sol, status=r.status_code)
    data = r.json() if r.status_code == 200 else {"error": r.status_code, "text": r.text[:400]}
    ops = []
    for o in data.get("opportunitiesData") or []:
        ops.append(
            {
                "noticeId": o.get("noticeId"),
                "solicitationNumber": o.get("solicitationNumber"),
                "title": o.get("title"),
                "typeOfSetAside": o.get("typeOfSetAside"),
                "typeOfSetAsideDescription": o.get("typeOfSetAsideDescription"),
                "responseDeadLine": o.get("responseDeadLine"),
                "postedDate": o.get("postedDate"),
                "uiLink": o.get("uiLink"),
                "naicsCode": o.get("naicsCode"),
                "classificationCode": o.get("classificationCode"),
            }
        )
    return {"http": r.status_code, "total": data.get("totalRecords"), "ops": ops}


def main() -> int:
    api_key = os.getenv("SAM_API_KEY") or os.getenv("SAM_GOV_API_KEY")
    print("sam_before", dashboard_payload().get("sam_calls_today"))
    print("has_key", bool(api_key))
    if not api_key:
        return 1
    out = {}
    for sol in ("N00104-27-Q-BA25", "SPE4A7-27-T-0317"):
        chk = check_budget(provider="sam", sam_calls=1)
        if not chk.get("allowed"):
            out[sol] = {"skipped": "budget", "detail": chk}
            continue
        out[sol] = search(sol, api_key)
        print(sol, out[sol].get("http"), out[sol].get("total"), out[sol].get("ops"))
    _save(
        "_og_commercial_research_pipeline_out/m3_adv_v1_sam_baseline_lookup.json",
        {"at": _utc(), "results": out, "budget_after": dashboard_payload()},
    )
    print("sam_after", dashboard_payload().get("sam_calls_today"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
