from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "euna_final_check.json"


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


def poll(c: httpx.Client, job_id: str, max_polls: int = 16) -> dict:
    last: dict = {}
    for i in range(max_polls):
        d = c.get(f"/api/m3/auth-jobs/{job_id}", timeout=30).json()
        print(f"poll {i}: {d.get('status')} {(d.get('progress') or {}).get('phase')}", flush=True)
        last = d
        if d.get("status") in {"COMPLETED", "FAILED"}:
            return d
        time.sleep(10)
    return last


def main() -> int:
    _load_dotenv()
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    report: dict = {"build": c.get("/api/health").json().get("build_version")}

    r = c.post("/api/m3/euna-auth/diagnostic", json={"async": True})
    report["diagnostic"] = poll(c, r.json()["job_id"])
    print("diag", json.dumps((report["diagnostic"].get("result") or {}), default=str)[:900], flush=True)

    r = c.post(
        "/api/m3/euna-discovery/run",
        json={"mode": "central", "max_results": 20, "max_pages": 2, "persist": True, "async": True},
    )
    report["harvest20"] = poll(c, r.json()["job_id"])
    print("harvest", json.dumps((report["harvest20"].get("result") or {}), default=str)[:1200], flush=True)

    report["euna_status"] = c.get("/api/m3/euna-auth/status").json()
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
