"""Production validation: BidNet authenticated harvest + OpenGov multi-step login.

Run on Railway after deploy. Never prints secrets.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    report: dict = {"kind": "AuthHarvestFixValidation", "stages": {}}

    # --- BidNet stage 1: 20 results ---
    print("=== BIDNET harvest n=20 ===", flush=True)
    from bidnet_discovery import run_bidnet_authenticated_harvest

    bn20 = run_bidnet_authenticated_harvest(
        max_results=20,
        max_pages=3,
        open_details=True,
        detail_limit=20,
        persist=True,
        use_auth=True,
    )
    h = bn20.get("harvest") or {}
    ds = h.get("detail_stats") or {}
    cm = bn20.get("canonical_merge") or {}
    report["stages"]["bidnet_20"] = {
        "auth": (bn20.get("auth") or {}).get("status"),
        "authenticated": (bn20.get("auth") or {}).get("authenticated"),
        "search_reachable": h.get("search_reachable"),
        "reported_total": h.get("reported_total"),
        "retrieved_total": h.get("retrieved_total"),
        "pagination_method": h.get("pagination_method"),
        "detail_stats": ds,
        "canonical": cm,
        "blocker": bn20.get("blocker"),
        "sample": bn20.get("records_sample"),
    }
    print(json.dumps(report["stages"]["bidnet_20"], indent=2, default=str), flush=True)

    # --- BidNet stage 2: 100 if stage1 ok ---
    if h.get("search_reachable") and (bn20.get("auth") or {}).get("authenticated"):
        print("=== BIDNET harvest n=100 ===", flush=True)
        bn100 = run_bidnet_authenticated_harvest(
            max_results=100,
            max_pages=8,
            open_details=True,
            detail_limit=100,
            persist=True,
            use_auth=True,
        )
        h100 = bn100.get("harvest") or {}
        report["stages"]["bidnet_100"] = {
            "auth": (bn100.get("auth") or {}).get("status"),
            "search_reachable": h100.get("search_reachable"),
            "reported_total": h100.get("reported_total"),
            "retrieved_total": h100.get("retrieved_total"),
            "pagination_method": h100.get("pagination_method"),
            "detail_stats": h100.get("detail_stats"),
            "canonical": bn100.get("canonical_merge"),
            "blocker": bn100.get("blocker"),
        }
        print(json.dumps(report["stages"]["bidnet_100"], indent=2, default=str), flush=True)
    else:
        report["stages"]["bidnet_100"] = {"skipped": True, "reason": "stage1_failed"}

    # --- OpenGov login + 10 portals ---
    print("=== OPENGOV test_connection ===", flush=True)
    from opengov_auth import test_connection

    og_auth = test_connection()
    # Strip any accidental secret-like keys
    for k in list(og_auth.keys()):
        if "pass" in k.lower() or "user" in k.lower() and k != "username_configured":
            if k in {"username", "password"}:
                og_auth.pop(k, None)
    report["stages"]["opengov_auth"] = {
        "status": og_auth.get("status"),
        "authenticated": og_auth.get("authenticated"),
        "message": og_auth.get("message"),
        "reused_session": og_auth.get("reused_session"),
        "details": og_auth.get("details"),
        "storage_state_present": og_auth.get("storage_state_present"),
    }
    print(json.dumps(report["stages"]["opengov_auth"], indent=2, default=str), flush=True)

    if og_auth.get("authenticated"):
        print("=== OPENGOV discovery max_entities=10 ===", flush=True)
        from opengov_discovery import run_opengov_authenticated_discovery

        og = run_opengov_authenticated_discovery(max_entities=10, max_pages=3, persist=True, use_auth=True)
        report["stages"]["opengov_10"] = {
            "auth": (og.get("auth") or {}).get("status"),
            "entities_attempted": og.get("entities_attempted"),
            "entities_successful": og.get("entities_successful"),
            "portal_status_counts": og.get("portal_status_counts"),
            "raw_opportunities": og.get("raw_opportunities"),
            "net_new": og.get("net_new"),
            "canonical_merge": og.get("canonical_merge"),
            "blocker": og.get("blocker"),
        }
        print(json.dumps(report["stages"]["opengov_10"], indent=2, default=str), flush=True)

        if (og.get("raw_opportunities") or 0) > 0:
            print("=== OPENGOV recovery limit=100 ===", flush=True)
            from opengov_recovery import run_opengov_recovery

            rec = run_opengov_recovery(limit=100, use_auth=True, persist=True, resume=True)
            report["stages"]["opengov_100"] = {
                "processed": rec.get("processed"),
                "stats": rec.get("stats"),
                "auth_stop": rec.get("auth_stop"),
            }
            print(json.dumps(report["stages"]["opengov_100"], indent=2, default=str), flush=True)
    else:
        report["stages"]["opengov_10"] = {"skipped": True, "reason": og_auth.get("message")}

    out_path = os.environ.get("M3_DATA_ROOT") or str(ROOT / "data")
    path = Path(out_path) / "auth_harvest_fix_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("WROTE", path, flush=True)
    print(json.dumps(report, indent=2, default=str)[:8000], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
