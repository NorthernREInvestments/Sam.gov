"""Chunked production validation to avoid Railway 502 proxy timeouts."""
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
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
        timeout=60,
    ).raise_for_status()
    print("login_ok", flush=True)
    report: dict = {}

    def harvest(**body):
        r = c.post("/api/m3/bidnet-discovery/harvest", json=body, timeout=480)
        print(f"harvest status={r.status_code} bytes={len(r.content)} body={body}", flush=True)
        if r.status_code >= 400 or not r.content:
            return {"error": r.status_code, "text": r.text[:300]}
        try:
            return r.json()
        except Exception:
            return {"error": "json", "text": r.text[:300]}

    # Stage A1: 100 list-only
    print("=== BN 100 list ===", flush=True)
    bn_list = harvest(max_results=100, max_pages=8, open_details=False, persist=True)
    h = bn_list.get("harvest") or {}
    report["bidnet_100_list"] = {
        "retrieved_total": h.get("retrieved_total"),
        "reported_total": h.get("reported_total"),
        "pages_scanned": h.get("pages_scanned"),
        "pagination_complete": h.get("pagination_complete"),
        "DISCOVERY_TRUNCATED": h.get("DISCOVERY_TRUNCATED"),
        "pagination_method": h.get("pagination_method"),
        "canonical_merge": bn_list.get("canonical_merge"),
        "error": bn_list.get("error") or h.get("error"),
    }
    print(json.dumps(report["bidnet_100_list"], indent=2, default=str), flush=True)

    # Stage A2: detail 20
    print("=== BN detail 20 ===", flush=True)
    bn_d = harvest(max_results=20, max_pages=2, open_details=True, detail_limit=20, persist=True)
    hd = bn_d.get("harvest") or {}
    report["bidnet_detail_20"] = {
        "retrieved_total": hd.get("retrieved_total"),
        "detail_stats": hd.get("detail_stats"),
        "detail_failure_counts": hd.get("detail_failure_counts"),
        "canonical_merge": bn_d.get("canonical_merge"),
        "error": bn_d.get("error") or hd.get("error"),
    }
    print(json.dumps(report["bidnet_detail_20"], indent=2, default=str), flush=True)

    # Stage B: 500 list in two 250 chunks if 100 worked
    if (h.get("retrieved_total") or 0) >= 50:
        print("=== BN 250 list #1 ===", flush=True)
        b1 = harvest(max_results=250, max_pages=15, open_details=False, persist=True)
        h1 = b1.get("harvest") or {}
        print("=== BN 250 list #2 (continues pagination from live UI) ===", flush=True)
        b2 = harvest(max_results=250, max_pages=15, open_details=False, persist=True)
        h2 = b2.get("harvest") or {}
        report["bidnet_500ish"] = {
            "chunk1": {
                "retrieved": h1.get("retrieved_total"),
                "pages": h1.get("pages_scanned"),
                "complete": h1.get("pagination_complete"),
                "truncated": h1.get("DISCOVERY_TRUNCATED"),
                "merge": b1.get("canonical_merge"),
            },
            "chunk2": {
                "retrieved": h2.get("retrieved_total"),
                "pages": h2.get("pages_scanned"),
                "complete": h2.get("pagination_complete"),
                "truncated": h2.get("DISCOVERY_TRUNCATED"),
                "merge": b2.get("canonical_merge"),
            },
        }
        print(json.dumps(report["bidnet_500ish"], indent=2, default=str), flush=True)

    print("=== OPENGOV ===", flush=True)
    oga = c.post("/api/m3/opengov-auth/test-connection", timeout=240).json()
    report["opengov_auth"] = {
        "status": oga.get("status"),
        "authenticated": oga.get("authenticated"),
        "reused_session": oga.get("reused_session"),
    }
    print(json.dumps(report["opengov_auth"], indent=2), flush=True)
    if oga.get("authenticated"):
        r = c.post(
            "/api/m3/opengov-discovery/run",
            json={"max_entities": 5, "max_pages": 2, "persist": True},
            timeout=480,
        )
        print("og_disc", r.status_code, len(r.content), flush=True)
        try:
            og = r.json()
            report["opengov"] = {
                "vendor_global_search": og.get("vendor_global_search"),
                "entities_attempted": og.get("entities_attempted"),
                "entities_successful": og.get("entities_successful"),
                "portal_status_counts": og.get("portal_status_counts"),
                "raw_opportunities": og.get("raw_opportunities"),
                "net_new": og.get("net_new"),
                "blocker": og.get("blocker"),
                "per_entity": {
                    k: {"status": v.get("status"), "raw": v.get("raw"), "error": v.get("error")}
                    for k, v in list((og.get("per_entity") or {}).items())[:8]
                },
            }
        except Exception:
            report["opengov"] = {"error": r.status_code, "text": r.text[:400]}
        print(json.dumps(report.get("opengov"), indent=2, default=str)[:4000], flush=True)

    report["bidnet_status"] = c.get("/api/m3/bidnet-auth/status", timeout=60).json()
    report["opengov_status"] = c.get("/api/m3/opengov-auth/status", timeout=60).json()
    out = ROOT / "data" / "production_harden_chunked.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("WROTE", out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
