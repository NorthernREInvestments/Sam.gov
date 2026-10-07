"""Kick money-path recovery once — no long polling."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
TARGET = "20261007-m3-money-path-recovery-v1"


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
    client = httpx.Client(base_url=BASE, timeout=45, follow_redirects=True)
    ready = False
    for i in range(30):
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
                f"wait {i}: build={env.get('build_version')} money={env.get('money_path_walker')}",
                flush=True,
            )
            if TARGET in str(env.get("build_version") or "") and int(env.get("money_path_walker") or 0) >= 1:
                ready = True
                break
        except Exception as exc:
            print(f"wait {i}: {type(exc).__name__}", flush=True)
        time.sleep(10)
    if not ready:
        print("deploy_not_ready", flush=True)
        return 2

    pre = client.get("/api/m3/bidnet-money/precheck")
    print("precheck", pre.status_code, pre.text[:400], flush=True)
    kicked = client.post(
        "/api/m3/bidnet-money/run",
        json={"canary_n": 20, "sprint_n": 100, "max_n": 250},
    )
    print("kick", kicked.status_code, kicked.text[:500], flush=True)
    if kicked.status_code != 200:
        return 1
    job = kicked.json()
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "m3_money_path_kick.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    print("JOB", job.get("job_id"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
