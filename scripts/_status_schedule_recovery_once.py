"""One-shot schedule recovery status."""
from __future__ import annotations

import json
import os
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


def main() -> None:
    _load_dotenv()
    client = httpx.Client(base_url=BASE, timeout=45, follow_redirects=True)
    login = client.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    )
    out = {"login": login.status_code}
    for path in (
        "/api/m3/schedule-recovery/status",
        "/api/m3/schedule-recovery/report",
        "/api/m3/bidnet-full-production/env-check",
    ):
        try:
            r = client.get(path)
            out[path] = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"status": r.status_code, "text": r.text[:500]}
        except Exception as exc:
            out[path] = {"error": f"{type(exc).__name__}:{exc}"}
    print(json.dumps(out, indent=2, default=str)[:14000])
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "m3_schedule_recovery_status_once.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
