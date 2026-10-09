"""Deploy-wait + production SAM ledger reconcile (0 live SAM API calls)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://samgov-production.up.railway.app"
TARGET = "20261009-m3-sam-api-credit-ledger-reconciliation-v1"
OUT = Path(__file__).resolve().parents[1] / "data" / "_tmp_sam_ledger_reconcile_prod.json"


def req(cookie: str, method: str, path: str, body: dict | None = None, timeout: int = 60):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            set_cookie = resp.headers.get("Set-Cookie") or ""
            new_cookie = set_cookie.split(";", 1)[0] if set_cookie else cookie
            return resp.status, json.loads(raw) if raw else {}, new_cookie
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"_raw": raw[:400]}
        return exc.code, payload, cookie
    except Exception as exc:
        return 0, {"_error": type(exc).__name__, "msg": str(exc)[:200]}, cookie


def main() -> int:
    email = (os.environ.get("APP_EMAIL") or "").strip()
    password = os.environ.get("APP_PASSWORD") or ""
    if not email or not password:
        print("MISSING_APP_CREDS", flush=True)
        return 4

    code, body, cookie = req("", "POST", "/api/login", {"email": email, "password": password})
    print("login", code, flush=True)
    if code != 200:
        return 5

    deadline = time.time() + 12 * 60
    build = None
    while time.time() < deadline:
        code, h, cookie = req(cookie, "GET", "/api/health", timeout=25)
        build = (h or {}).get("build_version")
        print(f"health {code} build={build}", flush=True)
        if build == TARGET:
            break
        time.sleep(20)
    else:
        print("DEPLOY_TIMEOUT", build, flush=True)
        return 2

    code, owner_before, cookie = req(cookie, "GET", "/api/m3/sam-credit-ledger/owner", timeout=60)
    print("owner_before", code, flush=True)
    code, recon, cookie = req(
        cookie, "POST", "/api/m3/sam-credit-ledger/reconcile", {"persist": True}, timeout=90
    )
    print("reconcile", code, flush=True)
    code, owner_after, cookie = req(cookie, "GET", "/api/m3/sam-credit-ledger/owner", timeout=60)
    print("owner_after", code, flush=True)

    report = {
        "build": build,
        "owner_before": owner_before,
        "reconcile": recon,
        "owner_after": owner_after,
    }
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    o = (recon or {}).get("owner") or owner_after or {}
    print(
        json.dumps(
            {
                "REAL_SAM_API_CALLS_TODAY": o.get("REAL_SAM_API_CALLS_TODAY"),
                "CACHE_HITS": o.get("CACHE_HITS"),
                "PUBLIC_NOTICEDESC_FETCHES": o.get("PUBLIC_NOTICEDESC_FETCHES"),
                "AGENCY_URL_FETCHES": o.get("AGENCY_URL_FETCHES"),
                "INTERNAL_ATTEMPTS": o.get("INTERNAL_ATTEMPTS"),
                "RETRIES": o.get("RETRIES"),
                "BLOCKED_OVER_CAP_ATTEMPTS": o.get("BLOCKED_OVER_CAP_ATTEMPTS"),
                "REMAINING_REAL_CREDITS": o.get("REMAINING_REAL_CREDITS"),
                "WHY_PRIOR_INFLATED": o.get("WHY_PRIOR_INFLATED"),
                "GATES": o.get("GATES") or (recon or {}).get("GATES"),
            },
            indent=2,
        ),
        flush=True,
    )
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
