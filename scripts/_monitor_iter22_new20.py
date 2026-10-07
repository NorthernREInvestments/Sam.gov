"""Poll iter-22 NEW-20 without restarting it. Exit when DONE/FAILED."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "m3_schedule_recovery_iter22_heartbeat.json"
MAX_WAIT_S = 3 * 3600
POLL_S = 45


def _load_env() -> None:
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
    _load_env()
    base = os.environ.get("APP_BASE_URL") or "https://samgov-production.up.railway.app"
    email = os.environ["APP_EMAIL"]
    password = os.environ["APP_PASSWORD"]
    started = time.time()
    last_key = None
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=base, timeout=45, follow_redirects=True) as c:
        c.post("/api/login", json={"email": email, "password": password})
        while time.time() - started < MAX_WAIT_S:
            st = c.get("/api/m3/schedule-recovery/status").json()
            key = (
                st.get("phase"),
                st.get("completed"),
                st.get("current_title"),
                st.get("updated_at"),
                st.get("STATUS"),
            )
            payload = {
                "polled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "elapsed_s": round(time.time() - started, 1),
                "status": st,
            }
            OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            if key != last_key:
                print(
                    f"HEARTBEAT completed={st.get('completed')}/{st.get('completed', 0) + st.get('remaining', 0)} "
                    f"phase={st.get('phase')} title={str(st.get('current_title') or st.get('last_title') or '')[:80]} "
                    f"updated={st.get('updated_at')}",
                    flush=True,
                )
                last_key = key
            phase = str(st.get("phase") or "")
            if phase == "DONE" or st.get("STATUS") in {"PASS", "FAIL", "SAME13_PASS_NEW20_FAIL"}:
                print("RUN_COMPLETE", json.dumps({k: st.get(k) for k in ("phase", "STATUS", "completed", "run_id", "iteration")}), flush=True)
                return 0
            if phase == "FAILED" or st.get("status") == "FAILED":
                print("RUN_FAILED", st, flush=True)
                return 2
            time.sleep(POLL_S)
    print("MONITOR_TIMEOUT", flush=True)
    return 3


if __name__ == "__main__":
    sys.exit(main())
