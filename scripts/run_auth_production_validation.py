"""Production auth validation for BidNet + OpenGov — never prints secrets.

Usage (against live Railway app):
  python scripts/run_auth_production_validation.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)

BASE = (os.environ.get("M3_VALIDATION_BASE_URL") or "https://samgov-production.up.railway.app").rstrip("/")
EMAIL = os.environ.get("APP_EMAIL") or ""
PASSWORD = os.environ.get("APP_PASSWORD") or ""

OUT = ROOT / "data" / "auth_production_validation_report.json"


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(x in kl for x in ("password", "cookie", "storage_state", "secret", "token")):
                out[k] = "[REDACTED]"
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    if isinstance(obj, str) and PASSWORD and PASSWORD in obj:
        return obj.replace(PASSWORD, "[REDACTED]")
    return obj


def login(client: httpx.Client) -> None:
    r = client.post(f"{BASE}/api/login", json={"email": EMAIL, "password": PASSWORD}, timeout=60)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok") and not data.get("token") and r.status_code >= 400:
        raise RuntimeError(f"login_failed status={r.status_code}")


def api(client: httpx.Client, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    r = client.request(method, f"{BASE}{path}", timeout=kwargs.pop("timeout", 600), **kwargs)
    try:
        body = r.json()
    except Exception:
        body = {"_raw": (r.text or "")[:500], "_status": r.status_code}
    if r.status_code >= 400:
        return {"ok": False, "http_status": r.status_code, "body": _scrub(body)}
    if isinstance(body, dict):
        body = _scrub(body)
        body.setdefault("ok", True)
        body["http_status"] = r.status_code
        return body
    return {"ok": True, "http_status": r.status_code, "body": body}


def main() -> int:
    if not EMAIL or not PASSWORD:
        print("FAIL: APP_EMAIL/APP_PASSWORD required for production API login")
        return 2

    report: dict[str, Any] = {
        "kind": "AuthProductionValidation",
        "base_url": BASE,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "railway_var_probe": {},
        "bidnet": {},
        "opengov": {},
        "scheduler": {},
        "canonical": {},
    }

    # Local probe of railway vars if available (presence only)
    try:
        import subprocess

        raw = subprocess.check_output(
            ["railway", "variables", "--json"],
            cwd=str(ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        vars_ = json.loads(raw)
        if isinstance(vars_, dict) and "variables" in vars_:
            vars_ = vars_["variables"]
        for k in (
            "BIDNET_USERNAME",
            "BIDNET_PASSWORD",
            "BIDNET_AUTH_ENABLED",
            "OPENGOV_USERNAME",
            "OPENGOV_PASSWORD",
            "OPENGOV_AUTH_ENABLED",
            "M3_DATA_ROOT",
            "BIDNET_HEADLESS",
            "OPENGOV_HEADLESS",
        ):
            v = vars_.get(k)
            if v is None or v == "":
                report["railway_var_probe"][k] = "MISSING"
            elif "PASSWORD" in k or "USERNAME" in k:
                report["railway_var_probe"][k] = f"SET(len={len(str(v))})"
            else:
                report["railway_var_probe"][k] = str(v)
    except Exception as exc:
        report["railway_var_probe"]["error"] = type(exc).__name__

    with httpx.Client(follow_redirects=True) as client:
        login(client)

        # --- BidNet auth ---
        print("BidNet: status…", flush=True)
        report["bidnet"]["status_before"] = api(client, "GET", "/api/m3/bidnet-auth/status", timeout=60)
        print("BidNet: test-connection…", flush=True)
        bn_conn = api(client, "POST", "/api/m3/bidnet-auth/test-connection", json={}, timeout=300)
        report["bidnet"]["test_connection"] = bn_conn
        report["bidnet"]["status_after"] = api(client, "GET", "/api/m3/bidnet-auth/status", timeout=60)
        # Session reuse: second test-connection should preferably SESSION_REUSED
        print("BidNet: session-reuse probe…", flush=True)
        bn_reuse = api(client, "POST", "/api/m3/bidnet-auth/test-connection", json={}, timeout=300)
        report["bidnet"]["session_reuse_probe"] = bn_reuse

        bn_ok = bool(
            (bn_conn.get("authenticated") if isinstance(bn_conn, dict) else False)
            or str(bn_conn.get("status") or "") in {"CONNECTED", "SESSION_REUSED", "LOGIN_SUCCESS"}
        )
        report["bidnet"]["auth_pass"] = bn_ok

        if bn_ok:
            print("BidNet: recovery 10…", flush=True)
            report["bidnet"]["recovery_10"] = api(
                client,
                "POST",
                "/api/m3/bidnet-recovery/run",
                json={"limit": 10, "use_auth": True, "resume": False, "force": True, "min_tier": 5},
                timeout=900,
            )
            print("BidNet: recovery 100…", flush=True)
            report["bidnet"]["recovery_100"] = api(
                client,
                "POST",
                "/api/m3/bidnet-recovery/run",
                json={"limit": 100, "use_auth": True, "resume": True, "force": False, "min_tier": 5},
                timeout=1800,
            )
        else:
            report["bidnet"]["recovery_10"] = {
                "skipped": True,
                "reason": bn_conn.get("status") or bn_conn.get("message") or bn_conn.get("body"),
            }
            report["bidnet"]["recovery_100"] = {"skipped": True, "reason": "auth_failed"}

        # --- OpenGov auth ---
        print("OpenGov: status…", flush=True)
        report["opengov"]["status_before"] = api(client, "GET", "/api/m3/opengov-auth/status", timeout=60)
        print("OpenGov: test-connection…", flush=True)
        og_conn = api(client, "POST", "/api/m3/opengov-auth/test-connection", json={}, timeout=300)
        report["opengov"]["test_connection"] = og_conn
        report["opengov"]["status_after"] = api(client, "GET", "/api/m3/opengov-auth/status", timeout=60)
        print("OpenGov: session-reuse probe…", flush=True)
        og_reuse = api(client, "POST", "/api/m3/opengov-auth/test-connection", json={}, timeout=300)
        report["opengov"]["session_reuse_probe"] = og_reuse

        og_ok = bool(
            (og_conn.get("authenticated") if isinstance(og_conn, dict) else False)
            or str(og_conn.get("status") or "") in {"CONNECTED", "SESSION_REUSED", "LOGIN_SUCCESS"}
        )
        report["opengov"]["auth_pass"] = og_ok

        if og_ok:
            print("OpenGov: discovery 10 portals…", flush=True)
            report["opengov"]["discovery_10"] = api(
                client,
                "POST",
                "/api/m3/opengov-discovery/run",
                json={"max_entities": 10, "max_pages": 5, "use_auth": True},
                timeout=1800,
            )
            print("OpenGov: recovery 100…", flush=True)
            report["opengov"]["recovery_100"] = api(
                client,
                "POST",
                "/api/m3/opengov-recovery/run",
                json={"limit": 100, "use_auth": True, "resume": True},
                timeout=1800,
            )
        else:
            report["opengov"]["discovery_10"] = {
                "skipped": True,
                "reason": og_conn.get("status") or og_conn.get("message") or og_conn.get("body"),
            }
            report["opengov"]["recovery_100"] = {"skipped": True, "reason": "auth_failed"}

        # Funnel snapshots
        report["bidnet"]["funnel"] = api(client, "GET", "/api/m3/bidnet-recovery/funnel", timeout=120)
        report["opengov"]["funnel"] = api(client, "GET", "/api/m3/opengov-recovery/funnel", timeout=120)

        # Scheduler-path controlled invocation (manual discovery run — same _execute_run path)
        print("Scheduler path: discovery run…", flush=True)
        report["scheduler"]["discovery_run"] = api(
            client,
            "POST",
            "/api/m3/discovery/run",
            json={},
            timeout=120,
        )
        time.sleep(5)
        report["scheduler"]["discovery_status"] = api(client, "GET", "/api/m3/discovery/status", timeout=60)
        if report["scheduler"]["discovery_status"].get("http_status") == 404:
            report["scheduler"]["discovery_health"] = api(client, "GET", "/api/health", timeout=60)

    report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {OUT}", flush=True)

    # Compact stdout summary (no secrets)
    print("SUMMARY", flush=True)
    print("BIDNET", bn_conn.get("status"), "auth=", bn_ok, flush=True)
    print("OPENGOV", og_conn.get("status"), "auth=", og_ok, flush=True)
    print("M3_DATA_ROOT", report["railway_var_probe"].get("M3_DATA_ROOT"), flush=True)
    return 0 if (bn_ok or og_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
