"""Staged OpenGov cascade validation: 10 → 50 → all. Bounded polls."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "opengov_cascade_validation.json"


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


def poll(c: httpx.Client, job_id: str, label: str, max_polls: int = 48) -> dict:
    print(f"poll_start {label} {job_id}", flush=True)
    last: dict = {}
    for i in range(max_polls):
        d = c.get(f"/api/m3/auth-jobs/{job_id}", timeout=30).json()
        p = d.get("progress") or {}
        print(
            f"poll {i}: status={d.get('status')} phase={p.get('phase')} "
            f"pct={p.get('pct')} entities={p.get('entities')} retrieved={p.get('retrieved')}",
            flush=True,
        )
        last = d
        if d.get("status") in {"COMPLETED", "FAILED"}:
            return d
        time.sleep(15)
    last["poll_timeout"] = True
    return last


def start_stage(c: httpx.Client, max_entities, *, persist: bool) -> dict:
    r = c.post(
        "/api/m3/opengov-discovery/run",
        json={
            "async": True,
            "mode": "cascade",
            "max_entities": max_entities,
            "max_pages": 20 if max_entities != "all" else 40,
            "persist": persist,
            "use_auth": True,
        },
        timeout=60,
    )
    print("start", max_entities, r.status_code, r.text[:240], flush=True)
    return r.json()


def summarize(res: dict) -> dict:
    r = res.get("result") or {}
    return {
        "status": res.get("status"),
        "attempted": r.get("entities_attempted"),
        "successful": r.get("entities_successful"),
        "route_counts": r.get("route_counts"),
        "portal_status_counts": r.get("portal_status_counts"),
        "anti_bot_primary": r.get("anti_bot_primary_failures"),
        "anti_bot_recovered": r.get("anti_bot_recovered_via_fallback"),
        "recovery_blocked": r.get("recovery_blocked"),
        "raw": r.get("raw_opportunities"),
        "unique": r.get("unique_records"),
        "net_new": r.get("net_new"),
        "vendor_global": r.get("vendor_global_search"),
        "blocked_sample": (r.get("blocked_entities") or [])[:15],
        "resolver": r.get("resolver_telemetry"),
    }


def main() -> int:
    _load_dotenv()
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    report: dict = {"build": c.get("/api/health").json().get("build_version"), "stages": {}}

    # Stage 10
    j = start_stage(c, 10, persist=True)
    s10 = poll(c, j["job_id"], "stage10", max_polls=24)
    report["stages"]["n10"] = summarize(s10)
    print("STAGE10", json.dumps(report["stages"]["n10"], default=str)[:900], flush=True)

    # Stage 50
    j = start_stage(c, 50, persist=True)
    s50 = poll(c, j["job_id"], "stage50", max_polls=40)
    report["stages"]["n50"] = summarize(s50)
    print("STAGE50", json.dumps(report["stages"]["n50"], default=str)[:900], flush=True)

    # Full universe
    j = start_stage(c, "all", persist=True)
    sall = poll(c, j["job_id"], "stage_all", max_polls=48)
    report["stages"]["all"] = summarize(sall)
    print("STAGE_ALL", json.dumps(report["stages"]["all"], default=str)[:1200], flush=True)

    try:
        report["resolver"] = c.get("/api/m3/opengov-discovery/resolver").json()
    except Exception as exc:
        report["resolver"] = {"error": type(exc).__name__}
    try:
        report["coverage"] = c.get("/api/m3/source-coverage/production").json()
    except Exception:
        pass

    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT, flush=True)
    return 0 if (report["stages"].get("all") or {}).get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
