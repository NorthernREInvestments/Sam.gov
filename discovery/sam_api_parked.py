"""SAM Opportunities API status — budgeted key (10 calls/day default).

Legacy park status preserved for tests; live ops use discovery.sam_budgeted_client.
"""

from __future__ import annotations

from typing import Any

SAM_API_PENDING_REPLACEMENT_KEY = "SAM_API_PENDING_REPLACEMENT_KEY"
SAM_API_BUDGETED = "SAM_API_BUDGETED"
SAM_OPPORTUNITIES_API_BASE = "https://api.sam.gov/opportunities/v2/search"

# Historical phase constant — live budget comes from SAM_DAILY_CALL_BUDGET / sam_budgeted_client
SAM_API_CALLS_ALLOWED_THIS_PHASE = 0


def sam_api_park_status() -> dict[str, Any]:
    """Compat status for phases/tests. Live usage: discovery.sam_budgeted_client.dashboard()."""
    key_present = False
    try:
        import os

        key_present = bool((os.getenv("SAM_GOV_API_KEY") or "").strip())
    except Exception:
        key_present = False
    used_today = 0
    limit = 10
    display = "SAM: 0/10 calls used · 10 remaining"
    try:
        from discovery.sam_budgeted_client import dashboard, sam_daily_call_budget

        d = dashboard()
        used_today = int(d.get("calls_used") or 0)
        limit = int(d.get("daily_limit") or sam_daily_call_budget())
        display = d.get("display") or display
    except Exception:
        pass
    return {
        "kind": "SamApiParkStatus",
        "status": SAM_API_BUDGETED if key_present else SAM_API_PENDING_REPLACEMENT_KEY,
        "reason": (
            f"Live Opportunities API allowed via discovery.sam_budgeted_client; "
            f"hard daily budget {limit} (SAM_DAILY_CALL_BUDGET)."
            if key_present
            else "SAM_GOV_API_KEY missing"
        ),
        "opportunities_api_base": SAM_OPPORTUNITIES_API_BASE,
        # Legacy field: phases assert this stays 0 for their own work (not ledger)
        "calls_allowed_this_phase": SAM_API_CALLS_ALLOWED_THIS_PHASE,
        "calls_consumed": 0,
        "calls_used_today": used_today,
        "daily_limit": limit,
        "display": display,
        "public_sam_search_unchanged": True,
        "canonical_client": "discovery.sam_budgeted_client",
        "note": "Do not call api.sam.gov outside discovery.sam_budgeted_client.",
    }


def assert_no_sam_opportunities_api_url(url: str | None) -> bool:
    if not url:
        return False
    u = str(url).lower()
    return "api.sam.gov/opportunities" in u or "api.sam.gov/prod/opportunities" in u


def block_sam_opportunities_fetch(url: str | None) -> dict[str, Any]:
    """Guard accidental ad-hoc Opportunities API fetches outside budgeted client."""
    if assert_no_sam_opportunities_api_url(url):
        return {
            "blocked": True,
            "status": "USE_sam_budgeted_client",
            "url": url,
            "calls_consumed": 0,
            "hint": "Use discovery.sam_budgeted_client.search_opportunities",
        }
    return {"blocked": False, "status": None, "calls_consumed": 0}
