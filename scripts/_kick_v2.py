"""Kick Source Repair V2 async jobs; write job ids."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "_v2_jobs.json"


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
    before = c.get("/api/m3/source-coverage/production").json()
    jobs = {
        "build": c.get("/api/health").json().get("build_version"),
        "before": before.get("combined"),
    }
    r = c.post(
        "/api/m3/bidnet-discovery/harvest",
        json={
            "max_results": 30000,
            "max_pages": 1200,
            "open_details": False,
            "persist": True,
            "async": True,
            "mode": "partitioned",
        },
    )
    jobs["bidnet"] = r.json()
    print("bidnet", r.status_code, r.text[:300], flush=True)

    r = c.post(
        "/api/m3/opengov-discovery/run",
        json={"max_entities": 200, "max_pages": 6, "persist": True, "async": True},
    )
    jobs["opengov"] = r.json()
    print("opengov", r.status_code, r.text[:300], flush=True)

    r = c.post(
        "/api/m3/euna-discovery/run",
        json={
            "mode": "central",
            "max_results": 10000,
            "max_pages": 50,
            "persist": True,
            "async": True,
        },
    )
    jobs["euna"] = r.json()
    print("euna", r.status_code, r.text[:300], flush=True)

    OUT.write_text(json.dumps(jobs, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
