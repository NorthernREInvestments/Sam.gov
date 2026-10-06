"""Production harden validation: BidNet 100/500 + OpenGov vendor/portals. No secrets."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k and k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")


def main() -> int:
    _load_dotenv()
    c = httpx.Client(base_url=BASE, timeout=None, follow_redirects=True)
    r = c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
        timeout=60,
    )
    r.raise_for_status()
    print("login_ok", flush=True)
    report: dict = {}

    print("=== BIDNET Stage A: 100 list+detail40 ===", flush=True)
    r = c.post(
        "/api/m3/bidnet-discovery/harvest",
        json={
            "max_results": 100,
            "max_pages": 8,
            "open_details": True,
            "detail_limit": 40,
            "persist": True,
        },
        timeout=1500,
    )
    print("bn100 status", r.status_code, "bytes", len(r.content), flush=True)
    try:
        bn100 = r.json()
    except Exception:
        print(r.text[:1500], flush=True)
        bn100 = {"error": "json_failed", "status_code": r.status_code}
    h = bn100.get("harvest") or {}
    report["bidnet_100"] = {
        "auth": (bn100.get("auth") or {}).get("status"),
        "reported_total": h.get("reported_total"),
        "retrieved_total": h.get("retrieved_total"),
        "unique_result_ids": h.get("unique_result_ids"),
        "pages_scanned": h.get("pages_scanned"),
        "pagination_complete": h.get("pagination_complete"),
        "DISCOVERY_TRUNCATED": h.get("DISCOVERY_TRUNCATED"),
        "pagination_method": h.get("pagination_method"),
        "detail_stats": h.get("detail_stats"),
        "detail_failure_counts": h.get("detail_failure_counts"),
        "canonical_merge": bn100.get("canonical_merge"),
    }
    print(json.dumps(report["bidnet_100"], indent=2, default=str)[:5000], flush=True)

    if (h.get("retrieved_total") or 0) >= 80:
        print("=== BIDNET Stage B: 500 list-only ===", flush=True)
        r = c.post(
            "/api/m3/bidnet-discovery/harvest",
            json={
                "max_results": 500,
                "max_pages": 30,
                "open_details": False,
                "persist": True,
            },
            timeout=1200,
        )
        print("bn500 status", r.status_code, "bytes", len(r.content), flush=True)
        try:
            bn500 = r.json()
            h5 = bn500.get("harvest") or {}
            report["bidnet_500"] = {
                "retrieved_total": h5.get("retrieved_total"),
                "pages_scanned": h5.get("pages_scanned"),
                "pagination_complete": h5.get("pagination_complete"),
                "DISCOVERY_TRUNCATED": h5.get("DISCOVERY_TRUNCATED"),
                "canonical_merge": bn500.get("canonical_merge"),
            }
        except Exception:
            report["bidnet_500"] = {"error": "json_failed", "status_code": r.status_code, "text": r.text[:400]}
        print(json.dumps(report["bidnet_500"], indent=2, default=str)[:3000], flush=True)

    print("=== OPENGOV auth ===", flush=True)
    r = c.post("/api/m3/opengov-auth/test-connection", timeout=240)
    oga = r.json()
    report["opengov_auth"] = {
        "status": oga.get("status"),
        "authenticated": oga.get("authenticated"),
        "reused_session": oga.get("reused_session"),
        "message": oga.get("message"),
    }
    print(json.dumps(report["opengov_auth"], indent=2), flush=True)

    if oga.get("authenticated"):
        print("=== OPENGOV discovery 10 ===", flush=True)
        r = c.post(
            "/api/m3/opengov-discovery/run",
            json={"max_entities": 10, "max_pages": 3, "persist": True},
            timeout=900,
        )
        print("og status", r.status_code, "bytes", len(r.content), flush=True)
        try:
            og = r.json()
            report["opengov_10"] = {
                "auth": (og.get("auth") or {}).get("status"),
                "vendor_global_search": og.get("vendor_global_search"),
                "entities_attempted": og.get("entities_attempted"),
                "entities_successful": og.get("entities_successful"),
                "portal_status_counts": og.get("portal_status_counts"),
                "raw_opportunities": og.get("raw_opportunities"),
                "net_new": og.get("net_new"),
                "canonical_merge": og.get("canonical_merge"),
                "blocker": og.get("blocker"),
                "per_entity": {
                    k: {"status": v.get("status"), "raw": v.get("raw"), "error": v.get("error")}
                    for k, v in list((og.get("per_entity") or {}).items())[:12]
                },
            }
        except Exception:
            report["opengov_10"] = {"error": "json_failed", "status_code": r.status_code, "text": r.text[:500]}
        print(json.dumps(report["opengov_10"], indent=2, default=str)[:5000], flush=True)

    # Connection status panel payloads
    report["bidnet_status"] = c.get("/api/m3/bidnet-auth/status", timeout=60).json()
    report["opengov_status"] = c.get("/api/m3/opengov-auth/status", timeout=60).json()

    out = ROOT / "data" / "production_harden_validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("WROTE", out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
