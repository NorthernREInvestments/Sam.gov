"""Quick poll current E2E job status."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
JID = "E2E-b8b6c8203aa8"


def main() -> None:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k and k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")
    c = httpx.Client(
        base_url="https://samgov-production.up.railway.app",
        timeout=45,
        follow_redirects=True,
    )
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    d = c.get(f"/api/m3/auth-jobs/{JID}").json()
    print(json.dumps({
        "status": d.get("status"),
        "progress": d.get("progress"),
        "error": d.get("error"),
        "updated_at": d.get("updated_at"),
        "has_result": bool(d.get("result")),
    }, indent=2))


if __name__ == "__main__":
    main()
