"""OpenGov 10 + BidNet 100 list-only (no detail open) to avoid proxy timeouts."""
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
    print("login", r.status_code, flush=True)

    print("=== OPENGOV auth ===", flush=True)
    r = c.post("/api/m3/opengov-auth/test-connection", timeout=240)
    oga = r.json()
    print(
        json.dumps(
            {k: oga.get(k) for k in ("status", "authenticated", "message", "reused_session")},
            indent=2,
        ),
        flush=True,
    )

    print("=== OPENGOV 10 ===", flush=True)
    r = c.post(
        "/api/m3/opengov-discovery/run",
        json={"max_entities": 10, "max_pages": 2, "persist": True},
        timeout=900,
    )
    print("og_status", r.status_code, flush=True)
    try:
        og = r.json()
    except Exception:
        print(r.text[:2000], flush=True)
        og = {}
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
                "per_entity": {
                    k: {
                        "status": v.get("status"),
                        "raw": v.get("raw"),
                        "error": v.get("error"),
                        "entity_name": v.get("entity_name"),
                    }
                    for k, v in list((og.get("per_entity") or {}).items())[:10]
                },
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )

    print("=== BIDNET 100 list-only ===", flush=True)
    r = c.post(
        "/api/m3/bidnet-discovery/harvest",
        json={
            "max_results": 100,
            "max_pages": 6,
            "open_details": False,
            "persist": True,
        },
        timeout=600,
    )
    print("bn_status", r.status_code, "bytes", len(r.content), flush=True)
    try:
        bn = r.json()
    except Exception:
        print(r.text[:2000], flush=True)
        return 1
    h = bn.get("harvest") or {}
    print(
        json.dumps(
            {
                "auth": (bn.get("auth") or {}).get("status"),
                "retrieved_total": h.get("retrieved_total"),
                "reported_total": h.get("reported_total"),
                "pages_scanned": h.get("pages_scanned"),
                "pagination_method": h.get("pagination_method"),
                "canonical_merge": bn.get("canonical_merge"),
                "sample_n": len(bn.get("records_sample") or []),
            },
            indent=2,
            default=str,
        ),
        flush=True,
    )

    # Open details for first 20 of those if list worked (incremental)
    if (h.get("retrieved_total") or 0) >= 20:
        print("=== BIDNET detail open 40 ===", flush=True)
        r = c.post(
            "/api/m3/bidnet-discovery/harvest",
            json={
                "max_results": 40,
                "max_pages": 3,
                "open_details": True,
                "detail_limit": 40,
                "persist": True,
            },
            timeout=1200,
        )
        try:
            bd = r.json()
            print(
                json.dumps(
                    {
                        "retrieved_total": (bd.get("harvest") or {}).get("retrieved_total"),
                        "detail_stats": (bd.get("harvest") or {}).get("detail_stats"),
                        "canonical_merge": bd.get("canonical_merge"),
                    },
                    indent=2,
                    default=str,
                ),
                flush=True,
            )
        except Exception:
            print("detail_json_failed", r.status_code, r.text[:500], flush=True)

    out = ROOT / "data" / "auth_harvest_fix_remainder.json"
    out.write_text(json.dumps({"og": og, "bn": bn}, indent=2, default=str)[:200000], encoding="utf-8")
    print("WROTE", out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
