"""Poll BidNet partitioned harvest job until complete."""
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
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()

    if "--kick" in sys.argv:
        r = c.post(
            "/api/m3/bidnet-discovery/harvest",
            json={"mode": "partitioned", "max_results": 30000, "resume": True},
        )
        print("kick", r.status_code, r.text[:400], flush=True)
        r.raise_for_status()
        job_id = r.json()["job_id"]
    else:
        job_id = None
        for a in sys.argv[1:]:
            if a.startswith("--job="):
                job_id = a.split("=", 1)[1]
        if not job_id:
            print("need --kick or --job=", flush=True)
            return 2

    print("job_id", job_id, flush=True)
    for i in range(600):
        d = c.get(f"/api/m3/auth-jobs/{job_id}").json()
        prog = d.get("progress") or {}
        print(
            f"poll {i}: status={d.get('status')} phase={prog.get('phase')} pct={prog.get('pct')} "
            f"retrieved={prog.get('retrieved')} pages={prog.get('pages')} "
            f"partition={prog.get('partition')} err={(d.get('error') or '')[:120]}",
            flush=True,
        )
        if d.get("status") in {"COMPLETED", "FAILED"}:
            out = ROOT / "data" / "m3_bidnet_partitioned_harvest_prod.json"
            out.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
            # also fetch coverage
            try:
                cov = c.get("/api/m3/source-coverage/production", timeout=90).json()
                print("owner_bidnet", json.dumps((cov.get("owner") or {}).get("bidnet"), default=str)[:800], flush=True)
            except Exception as exc:
                print("cov_err", type(exc).__name__, flush=True)
            print("FINAL", d.get("status"), flush=True)
            return 0 if d.get("status") == "COMPLETED" else 1
        time.sleep(30)
    print("poll_timeout", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
