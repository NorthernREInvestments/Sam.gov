"""Staged Euna validation A→D (bounded polls)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "euna_stage_validation.json"


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


def poll(c: httpx.Client, job_id: str, label: str, max_polls: int = 16) -> dict:
    print(f"poll_start {label} {job_id}", flush=True)
    last: dict = {}
    for i in range(max_polls):
        try:
            d = c.get(f"/api/m3/auth-jobs/{job_id}", timeout=30).json()
        except Exception as exc:
            print(f"poll {i}: err={type(exc).__name__}", flush=True)
            time.sleep(15)
            continue
        p = d.get("progress") or {}
        print(
            f"poll {i}: status={d.get('status')} phase={p.get('phase')} "
            f"pct={p.get('pct')} retrieved={p.get('retrieved')}",
            flush=True,
        )
        last = d
        if d.get("status") in {"COMPLETED", "FAILED"}:
            return d
        time.sleep(15)
    last["poll_timeout"] = True
    return last


def main() -> int:
    _load_dotenv()
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    report: dict = {"build": c.get("/api/health").json().get("build_version")}

    # Stage A — AUTH DIAGNOSTIC
    r = c.post("/api/m3/euna-auth/diagnostic", json={"async": True})
    print("stage_a", r.status_code, r.text[:300], flush=True)
    jid = r.json().get("job_id")
    report["stage_a"] = poll(c, jid, "auth_diag", max_polls=16) if jid else r.json()

    ares = (report.get("stage_a") or {}).get("result") or {}
    login_ok = bool(ares.get("login_success"))
    print("stage_a_result", json.dumps(ares, default=str)[:800], flush=True)

    if not login_ok:
        report["stopped_after"] = "stage_a"
        report["coverage"] = c.get("/api/m3/source-coverage/production").json()
        report["euna_status"] = c.get("/api/m3/euna-auth/status").json()
        OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print("AUTH_BLOCKED", ares.get("failure_reason"), flush=True)
        return 2

    # Stage B/C — LOGIN SUCCESS already proven; harvest 20
    r = c.post(
        "/api/m3/euna-discovery/run",
        json={"mode": "central", "max_results": 20, "max_pages": 3, "persist": True, "async": True},
    )
    print("stage_c", r.status_code, r.text[:300], flush=True)
    jid = r.json().get("job_id")
    report["stage_c_20"] = poll(c, jid, "euna_20", max_polls=20) if jid else r.json()

    # Stage D — 100
    r = c.post(
        "/api/m3/euna-discovery/run",
        json={"mode": "central", "max_results": 100, "max_pages": 8, "persist": True, "async": True},
    )
    print("stage_d", r.status_code, r.text[:300], flush=True)
    jid = r.json().get("job_id")
    report["stage_d_100"] = poll(c, jid, "euna_100", max_polls=20) if jid else r.json()

    report["coverage"] = c.get("/api/m3/source-coverage/production").json()
    report["euna_status"] = c.get("/api/m3/euna-auth/status").json()
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT, flush=True)
    print(
        "SUMMARY",
        json.dumps(
            {
                "failure": ares.get("failure_reason"),
                "login": login_ok,
                "c20": ((report.get("stage_c_20") or {}).get("result") or {}).get("retrieved_total"),
                "d100": ((report.get("stage_d_100") or {}).get("result") or {}).get("retrieved_total"),
                "net_new": ((report.get("coverage") or {}).get("owner") or {}).get("euna", {}).get("net_new"),
            },
            default=str,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
