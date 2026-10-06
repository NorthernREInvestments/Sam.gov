"""Bounded poll for one auth job. Usage: python scripts/_poll_job.py JOB_ID OUT.json [max_polls]"""
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
    job_id = sys.argv[1]
    out = Path(sys.argv[2])
    max_polls = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    _load_dotenv()
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    last: dict = {}
    for i in range(max_polls):
        try:
            d = c.get(f"/api/m3/auth-jobs/{job_id}", timeout=30).json()
        except Exception as exc:
            print(f"poll {i}: transport_error={type(exc).__name__}", flush=True)
            time.sleep(POLL_SEC if False else 15)
            continue
        p = d.get("progress") or {}
        print(
            f"poll {i}: status={d.get('status')} phase={p.get('phase')} "
            f"pct={p.get('pct')} retrieved={p.get('retrieved')} "
            f"partition={p.get('partition')} done={p.get('partitions_done')}",
            flush=True,
        )
        last = d
        if d.get("status") in {"COMPLETED", "FAILED"}:
            break
        time.sleep(15)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(last, indent=2, default=str), encoding="utf-8")
    print("saved", out, last.get("status"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
