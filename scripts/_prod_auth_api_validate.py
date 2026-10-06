"""Call production auth/harvest APIs using local .env login. Never prints secrets."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("M3_PROD_BASE", "https://samgov-production.up.railway.app")


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
    email = os.environ.get("APP_EMAIL") or ""
    password = os.environ.get("APP_PASSWORD") or ""
    if not email or not password:
        print("APP_EMAIL/APP_PASSWORD missing", flush=True)
        return 2

    client = httpx.Client(base_url=BASE, timeout=None, follow_redirects=True)
    r = client.post("/api/login", json={"email": email, "password": password}, timeout=60)
    if r.status_code >= 400:
        print("login_failed", r.status_code, flush=True)
        return 1
    print("login_ok", flush=True)

    report: dict = {}

    print("=== OPENGOV test-connection ===", flush=True)
    r = client.post("/api/m3/opengov-auth/test-connection", timeout=240)
    report["opengov_auth"] = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"status_code": r.status_code}
    # scrub
    for k in ("username", "password"):
        if isinstance(report["opengov_auth"], dict):
            report["opengov_auth"].pop(k, None)
    print(json.dumps(report["opengov_auth"], indent=2, default=str)[:2500], flush=True)

    print("=== BIDNET harvest n=20 ===", flush=True)
    r = client.post(
        "/api/m3/bidnet-discovery/harvest",
        json={"max_results": 20, "max_pages": 3, "open_details": True, "detail_limit": 20, "persist": True},
        timeout=900,
    )
    report["bidnet_20"] = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"status_code": r.status_code, "text": r.text[:500]}
    # compact
    compact = {
        "auth": (report["bidnet_20"].get("auth") if isinstance(report["bidnet_20"], dict) else None),
        "harvest": (report["bidnet_20"].get("harvest") if isinstance(report["bidnet_20"], dict) else None),
        "canonical_merge": (report["bidnet_20"].get("canonical_merge") if isinstance(report["bidnet_20"], dict) else None),
        "blocker": (report["bidnet_20"].get("blocker") if isinstance(report["bidnet_20"], dict) else None),
        "sample": (report["bidnet_20"].get("records_sample") if isinstance(report["bidnet_20"], dict) else None),
    }
    print(json.dumps(compact, indent=2, default=str)[:4000], flush=True)

    og_ok = isinstance(report.get("opengov_auth"), dict) and report["opengov_auth"].get("authenticated")
    if og_ok:
        print("=== OPENGOV discovery n=10 ===", flush=True)
        r = client.post(
            "/api/m3/opengov-discovery/run",
            json={"max_entities": 10, "max_pages": 3, "persist": True},
            timeout=900,
        )
        og = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"status_code": r.status_code}
        report["opengov_10"] = {
            "auth": (og.get("auth") or {}).get("status") if isinstance(og, dict) else None,
            "entities_attempted": og.get("entities_attempted") if isinstance(og, dict) else None,
            "entities_successful": og.get("entities_successful") if isinstance(og, dict) else None,
            "portal_status_counts": og.get("portal_status_counts") if isinstance(og, dict) else None,
            "raw_opportunities": og.get("raw_opportunities") if isinstance(og, dict) else None,
            "net_new": og.get("net_new") if isinstance(og, dict) else None,
            "canonical_merge": og.get("canonical_merge") if isinstance(og, dict) else None,
            "blocker": og.get("blocker") if isinstance(og, dict) else None,
        }
        print(json.dumps(report["opengov_10"], indent=2, default=str)[:3000], flush=True)

    # BidNet 100 if 20 worked
    h = compact.get("harvest") or {}
    if h.get("search_reachable") and (compact.get("auth") or {}).get("authenticated"):
        print("=== BIDNET harvest n=100 ===", flush=True)
        r = client.post(
            "/api/m3/bidnet-discovery/harvest",
            json={"max_results": 100, "max_pages": 8, "open_details": True, "detail_limit": 100, "persist": True},
            timeout=1800,
        )
        bn = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"status_code": r.status_code}
        report["bidnet_100"] = {
            "auth": (bn.get("auth") or {}).get("status") if isinstance(bn, dict) else None,
            "harvest": bn.get("harvest") if isinstance(bn, dict) else None,
            "canonical_merge": bn.get("canonical_merge") if isinstance(bn, dict) else None,
            "blocker": bn.get("blocker") if isinstance(bn, dict) else None,
        }
        print(json.dumps(report["bidnet_100"], indent=2, default=str)[:4000], flush=True)

    out = ROOT / "data" / "auth_harvest_fix_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("WROTE", out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
