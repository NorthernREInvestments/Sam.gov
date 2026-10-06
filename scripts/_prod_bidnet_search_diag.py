"""Diagnose BidNet authenticated search page structure in production. No secrets."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("M3_PROD_BASE", "https://samgov-production.up.railway.app")


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
    client = httpx.Client(base_url=BASE, timeout=300, follow_redirects=True)
    r = client.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
        timeout=60,
    )
    r.raise_for_status()
    url = "https://www.bidnetdirect.com/private/supplier/solicitations/search"
    r = client.post("/api/m3/bidnet-auth/debug-fetch", json={"url": url}, timeout=240)
    print(r.text[:4000], flush=True)

    # Also hit a small custom diagnostic via harvest with open_details false after we enhance endpoint
    # For now: reuse debug-fetch on public open for comparison
    r2 = client.post(
        "/api/m3/bidnet-auth/debug-fetch",
        json={"url": "https://www.bidnetdirect.com/public/solicitations/open"},
        timeout=240,
    )
    print("PUBLIC", r2.text[:2000], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
