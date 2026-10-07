"""Poll schedule recovery until DONE/FAILED; print compact progress."""
from __future__ import annotations

import json
import os
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
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def main() -> int:
    _load_dotenv()
    client = httpx.Client(base_url=BASE, timeout=45, follow_redirects=True)
    for i in range(60):  # up to ~20 min at 20s
        client.post(
            "/api/login",
            json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
        )
        st = client.get("/api/m3/schedule-recovery/status").json()
        phase = st.get("phase") or st.get("status")
        print(
            f"poll {i}: phase={phase} pct={st.get('percent')} "
            f"done={st.get('completed')} auth={st.get('AUTHORITATIVE')} lines={st.get('LINES')} "
            f"STATUS={st.get('STATUS')} run={st.get('run_id')}",
            flush=True,
        )
        if phase in {"DONE", "FAILED"} or st.get("STATUS") in {"PASS", "FAIL", "SAME13_PASS_NEW20_FAIL"}:
            rep = client.get("/api/m3/schedule-recovery/report").json()
            (ROOT / "data").mkdir(exist_ok=True)
            (ROOT / "data" / "m3_schedule_recovery_last_poll.json").write_text(
                json.dumps({"status": st, "report": rep}, indent=2, default=str),
                encoding="utf-8",
            )
            after = rep.get("after") or {}
            gates = rep.get("gates") or {}
            print("AFTER", json.dumps(after, default=str)[:2000], flush=True)
            print("GATES", json.dumps(gates, default=str), flush=True)
            print("DOWNSTREAM", json.dumps(rep.get("downstream") or {}, default=str), flush=True)
            print("STATUS", rep.get("STATUS"), "NEW20", (rep.get("NEW_20") or {}).get("RUN"), flush=True)
            return 0 if rep.get("STATUS") in {"PASS", "SAME13_PASS_NEW20_FAIL"} else 1
        time.sleep(20)
    print("poll_timeout", flush=True)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
