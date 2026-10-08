"""Kick a 2-opp SAME-20 debug canary (15m budget). Never runs the full 20."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
DEFAULT_KEYS = ["b40059ab7b97fcd6", "ccb8dfe0e5772614"]


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
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    keys = [k for k in sys.argv[2:] if k.strip()] or DEFAULT_KEYS
    client = httpx.Client(base_url=BASE, timeout=45, follow_redirects=True)
    for i in range(36):
        try:
            login = client.post(
                "/api/login",
                json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
            )
            if login.status_code != 200:
                print(f"wait {i}: login={login.status_code}", flush=True)
                time.sleep(8)
                continue
            health = client.get("/api/health").json()
            commit = str(health.get("git_commit") or "")[:12]
            st = client.get("/api/m3/same20-full-pipeline/status").json()
            phase = st.get("phase") or st.get("status")
            print(f"wait {i}: commit={commit} phase={phase}", flush=True)
            if want and want[:7] not in commit:
                time.sleep(8)
                continue
            if str(phase or "").startswith("SAME20_") and phase not in {
                "SAME20_DONE",
                "SAME20_FAIL",
                "SAME20_PASS",
                "SAME20_BUDGET_STOP",
                "DONE",
                "FAIL",
                "STALLED",
            }:
                print("busy", phase, flush=True)
                time.sleep(10)
                continue
            break
        except Exception as exc:
            print(f"wait {i}: {type(exc).__name__}", flush=True)
            time.sleep(8)
    else:
        print("deploy_not_ready", flush=True)
        return 2

    kicked = client.post(
        "/api/m3/same20-full-pipeline/run",
        json={
            "mode": "same20_canary",
            "stable_keys": keys,
            "max_opps": len(keys),
            "run_budget_s": 15 * 60,
            "iteration": 90,
            "change_made": "debug canary 2-opp",
            "warm_cache": False,
            "price_budget": 10,
        },
    )
    print("kick", kicked.status_code, kicked.text[:500], flush=True)
    if kicked.status_code != 200:
        return 1
    job = kicked.json()
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "m3_same20_canary_kick.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    print("JOB", job.get("job_id"), "keys", keys, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
