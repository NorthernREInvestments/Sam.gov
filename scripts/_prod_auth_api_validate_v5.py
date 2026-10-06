"""Focused v5 validation: BidNet 20 (+100 if ok) and OpenGov 10 portals."""
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
    r = c.post("/api/login", json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]}, timeout=60)
    r.raise_for_status()
    print("login_ok", flush=True)

    print("=== BIDNET 20 ===", flush=True)
    r = c.post(
        "/api/m3/bidnet-discovery/harvest",
        json={"max_results": 20, "max_pages": 3, "open_details": True, "detail_limit": 20, "persist": True},
        timeout=1200,
    )
    bn = r.json()
    h = bn.get("harvest") or {}
    print(
        json.dumps(
            {
                "auth": (bn.get("auth") or {}).get("status"),
                "search_reachable": h.get("search_reachable"),
                "reported_total": h.get("reported_total"),
                "retrieved_total": h.get("retrieved_total"),
                "pagination_method": h.get("pagination_method"),
                "detail_stats": h.get("detail_stats"),
                "diag": h.get("diag"),
                "diag_row0_html": h.get("diag_row0_html"),
                "fallback_url": h.get("fallback_url"),
                "canonical_merge": bn.get("canonical_merge"),
                "sample": bn.get("records_sample"),
            },
            indent=2,
            default=str,
        )[:6000],
        flush=True,
    )

    if (h.get("retrieved_total") or 0) > 0:
        print("=== BIDNET 100 ===", flush=True)
        r = c.post(
            "/api/m3/bidnet-discovery/harvest",
            json={"max_results": 100, "max_pages": 8, "open_details": True, "detail_limit": 100, "persist": True},
            timeout=2400,
        )
        bn100 = r.json()
        print(
            json.dumps(
                {
                    "retrieved_total": (bn100.get("harvest") or {}).get("retrieved_total"),
                    "detail_stats": (bn100.get("harvest") or {}).get("detail_stats"),
                    "canonical_merge": bn100.get("canonical_merge"),
                    "pagination_method": (bn100.get("harvest") or {}).get("pagination_method"),
                },
                indent=2,
                default=str,
            )[:4000],
            flush=True,
        )

    print("=== OPENGOV auth ===", flush=True)
    r = c.post("/api/m3/opengov-auth/test-connection", timeout=240)
    oga = r.json()
    print(json.dumps({k: oga.get(k) for k in ("status", "authenticated", "message", "reused_session", "details")}, indent=2, default=str)[:2500], flush=True)

    if oga.get("authenticated"):
        print("=== OPENGOV 10 ===", flush=True)
        r = c.post("/api/m3/opengov-discovery/run", json={"max_entities": 10, "max_pages": 2, "persist": True}, timeout=1200)
        og = r.json()
        print(
            json.dumps(
                {
                    "auth": (og.get("auth") or {}).get("status"),
                    "entities_attempted": og.get("entities_attempted"),
                    "entities_successful": og.get("entities_successful"),
                    "portal_status_counts": og.get("portal_status_counts"),
                    "raw_opportunities": og.get("raw_opportunities"),
                    "net_new": og.get("net_new"),
                    "blocker": og.get("blocker"),
                    "per_entity_sample": dict(list((og.get("per_entity") or {}).items())[:5]),
                },
                indent=2,
                default=str,
            )[:4000],
            flush=True,
        )

    out = ROOT / "data" / "auth_harvest_fix_v5.json"
    out.write_text("done", encoding="utf-8")
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
