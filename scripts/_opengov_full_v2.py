"""Full OpenGov cascade pass (all known entities) — bounded poll."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "opengov_cascade_full_v2.json"


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
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    print("build", c.get("/api/health").json().get("build_version"), flush=True)
    r = c.post(
        "/api/m3/opengov-discovery/run",
        json={
            "async": True,
            "mode": "cascade",
            "max_entities": "all",
            "max_pages": 40,
            "persist": True,
            "use_auth": True,
            "allow_browser": False,
        },
    )
    print("start", r.status_code, r.text[:300], flush=True)
    jid = r.json()["job_id"]
    last = {}
    for i in range(60):  # up to ~15 min
        d = c.get(f"/api/m3/auth-jobs/{jid}", timeout=30).json()
        p = d.get("progress") or {}
        print(
            f"poll {i}: {d.get('status')} {p.get('phase')} pct={p.get('pct')} "
            f"ent={p.get('entities')} ret={p.get('retrieved')}",
            flush=True,
        )
        last = d
        if d.get("status") in {"COMPLETED", "FAILED"}:
            break
        time.sleep(15)
    res = last.get("result") or {}
    summary = {
        "status": last.get("status"),
        "attempted": res.get("entities_attempted"),
        "successful": res.get("entities_successful"),
        "route_counts": res.get("route_counts"),
        "portal_status_counts": res.get("portal_status_counts"),
        "anti_bot_primary": res.get("anti_bot_primary_failures"),
        "anti_bot_recovered": res.get("anti_bot_recovered_via_fallback"),
        "recovery_blocked": res.get("recovery_blocked"),
        "raw": res.get("raw_opportunities"),
        "unique": res.get("unique_records"),
        "net_new": res.get("net_new"),
        "existing_enriched": (res.get("canonical_merge") or {}).get("existing_enriched")
        or (res.get("canonical_merge") or {}).get("updated"),
        "vendor_global": {
            k: (res.get("vendor_global_search") or {}).get(k)
            for k in (
                "search_reachable",
                "search_url",
                "retrieved_total",
                "reported_total",
                "error",
            )
        },
        "blocked_sample": (res.get("blocked_entities") or [])[:25],
        "resolver": res.get("resolver_telemetry"),
        "per_entity_sample": dict(list((res.get("per_entity") or {}).items())[:20]),
    }
    OUT.write_text(json.dumps({"build": "v2", "summary": summary, "job": last}, indent=2, default=str), encoding="utf-8")
    print("SUMMARY", json.dumps(summary, default=str)[:2500], flush=True)
    print("wrote", OUT, flush=True)
    return 0 if last.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
