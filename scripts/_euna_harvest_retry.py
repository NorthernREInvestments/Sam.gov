"""Retry Euna central harvest after SPA extraction fix."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "euna_harvest_retry.json"


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


def poll(c: httpx.Client, job_id: str, max_polls: int = 20) -> dict:
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
            f"retrieved={p.get('retrieved')} pct={p.get('pct')}",
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

    r = c.post(
        "/api/m3/euna-discovery/run",
        json={"mode": "central", "max_results": 100, "max_pages": 10, "persist": True, "async": True},
    )
    print("start", r.status_code, r.text[:300], flush=True)
    jid = r.json().get("job_id")
    report["job"] = poll(c, jid) if jid else r.json()
    report["coverage"] = c.get("/api/m3/source-coverage/production").json()
    report["status"] = c.get("/api/m3/euna-auth/status").json()
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    res = (report.get("job") or {}).get("result") or {}
    print(
        "RESULT",
        json.dumps(
            {
                "auth": res.get("auth"),
                "search_url": res.get("search_url"),
                "retrieved": res.get("retrieved_total"),
                "reported": res.get("reported_total"),
                "net_new": res.get("net_new"),
                "blocker": res.get("blocker"),
                "nav": res.get("nav_clicked"),
                "apis": res.get("api_urls_seen"),
                "sample": res.get("page_text_sample"),
            },
            default=str,
        )[:2000],
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
