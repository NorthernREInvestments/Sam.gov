"""One-shot schedule-backed canary status."""
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


def main() -> int:
    _load_dotenv()
    client = httpx.Client(base_url=BASE, timeout=30, follow_redirects=True)
    client.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    kick = ROOT / "data" / "m3_schedule_backed_canary_kick.json"
    job_id = json.loads(kick.read_text(encoding="utf-8")).get("job_id") if kick.exists() else None
    for path in (
        "/api/health",
        f"/api/m3/auth-jobs/{job_id}" if job_id else None,
        "/api/m3/schedule-backed/canary/status",
        "/api/m3/schedule-backed/canary/report",
    ):
        if not path:
            continue
        r = client.get(path)
        print(f"{path} {r.status_code} {r.text[:900].replace(chr(10), ' ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
