from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "euna_api_probe_result.json"


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
    r = c.post("/api/m3/euna-discovery/api-probe", json={})
    print("start", r.status_code, r.text[:300], flush=True)
    jid = r.json().get("job_id")
    last = {}
    for i in range(20):
        d = c.get(f"/api/m3/auth-jobs/{jid}", timeout=30).json()
        print(f"poll {i}: {d.get('status')} {(d.get('progress') or {}).get('phase')}", flush=True)
        last = d
        if d.get("status") in {"COMPLETED", "FAILED"}:
            break
        time.sleep(15)
    OUT.write_text(json.dumps(last, indent=2, default=str), encoding="utf-8")
    print(json.dumps(last.get("result"), indent=2, default=str)[:4000], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
