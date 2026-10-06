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
        k, _, v = line.partition("=")
        k = k.strip()
        if k and k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")


def main() -> None:
    _load_dotenv()
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    for jid in ("EUD-a24d75445dca", "EUD-2d82062afc7e"):
        d = c.get(f"/api/m3/auth-jobs/{jid}").json()
        print("===", jid, d.get("status"))
        print(json.dumps(d.get("result"), indent=2, default=str)[:2000])
    cov = c.get("/api/m3/source-coverage/production").json()
    eu = cov.get("euna_last_discovery") or {}
    print("owner", json.dumps((cov.get("owner") or {}).get("euna"), indent=2, default=str))
    print(
        "last_discovery",
        json.dumps(
            {
                "blocker": eu.get("blocker"),
                "search_url": eu.get("search_url"),
                "pages": eu.get("pages_scanned"),
                "retrieved": eu.get("retrieved_total"),
                "error": eu.get("error") if "error" in eu else None,
                "auth": eu.get("auth"),
                "central_reachable": eu.get("central_reachable"),
            },
            indent=2,
            default=str,
        ),
    )


if __name__ == "__main__":
    main()
