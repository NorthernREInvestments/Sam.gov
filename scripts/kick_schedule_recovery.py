"""Kick same-13 authoritative schedule recovery — no long polling."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
TARGET = "20261007-m3-authoritative-schedule-recovery-v1"


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def main() -> int:
    _load_dotenv()
    iteration = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    change = sys.argv[2] if len(sys.argv) > 2 else "content-first schedule recognition"
    client = httpx.Client(base_url=BASE, timeout=45, follow_redirects=True)
    ready = False
    for i in range(36):
        try:
            login = client.post(
                "/api/login",
                json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
            )
            if login.status_code != 200:
                print(f"wait {i}: login={login.status_code}", flush=True)
                time.sleep(10)
                continue
            env = client.get("/api/m3/bidnet-full-production/env-check").json()
            print(
                f"wait {i}: build={env.get('build_version')} sr={env.get('schedule_recovery_walker')}",
                flush=True,
            )
            if TARGET in str(env.get("build_version") or "") and int(env.get("schedule_recovery_walker") or 0) >= 1:
                ready = True
                break
        except Exception as exc:
            print(f"wait {i}: {type(exc).__name__}", flush=True)
        time.sleep(10)
    if not ready:
        print("deploy_not_ready", flush=True)
        return 2

    kicked = client.post(
        "/api/m3/schedule-recovery/run",
        json={
            "mode": "same_13",
            "price_budget": 25,
            "iteration": iteration,
            "change_made": change,
        },
    )
    print("kick", kicked.status_code, kicked.text[:600], flush=True)
    if kicked.status_code != 200:
        return 1
    job = kicked.json()
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "m3_schedule_recovery_kick.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    print("JOB", job.get("job_id"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
