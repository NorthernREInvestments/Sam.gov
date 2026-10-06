"""Wait for Railway health, then kick partitioned BidNet harvest only."""
from __future__ import annotations

import json
import os
import sys
import time
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
    c = httpx.Client(base_url=BASE, timeout=120, follow_redirects=True)

    for i in range(40):
        try:
            r = c.get("/api/health")
            print(f"health {i}: {r.status_code} {r.text[:180]}", flush=True)
            if r.status_code == 200:
                break
        except Exception as exc:
            print(f"health {i}: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(8)
    else:
        print("health_timeout", flush=True)
        return 2

    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()

    ec = c.get("/api/m3/bidnet-full-production/env-check")
    print("env", ec.status_code, ec.text[:600], flush=True)

    # Confirm the wasteful BNFP redo is gone after redeploy
    for jid in ["BNFP-7b9d99c42c4f", "BNH-d1509c2df362"]:
        try:
            d = c.get(f"/api/m3/auth-jobs/{jid}").json()
            print(
                f"old_job {jid}: status={d.get('status')} phase={(d.get('progress') or {}).get('phase')}",
                flush=True,
            )
        except Exception as exc:
            print(f"old_job {jid}: {type(exc).__name__}", flush=True)

    if "--kick" not in sys.argv:
        return 0

    r = c.post(
        "/api/m3/bidnet-discovery/harvest",
        json={"mode": "partitioned", "max_results": 30000, "resume": True},
    )
    print("kick", r.status_code, r.text[:500], flush=True)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    out = ROOT / "data" / "m3_bidnet_partitioned_harvest_job_id.txt"
    out.write_text(job_id, encoding="utf-8")
    print("job_id", job_id, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
