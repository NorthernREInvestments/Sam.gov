"""Launch full-scale production funnel on Railway (background worker via API).

Cursor: run this script, confirm STARTED + heartbeat, then return.
The worker continues inside the Railway web process independently of Cursor.

Usage (from govtracker/):
  python scripts/start_full_scale_production_funnel_v1.py
  python scripts/start_full_scale_production_funnel_v1.py --rebuild-queue
  python scripts/start_full_scale_production_funnel_v1.py --poll-seconds 45
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

BASE = (os.environ.get("M3_PRODUCTION_BASE") or "https://samgov-production.up.railway.app").rstrip("/")


def _req(cookie: str, method: str, path: str, body: dict | None = None, timeout: float = 120.0):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode()
            sc = resp.headers.get("Set-Cookie") or ""
            cookie_out = sc.split(";", 1)[0] if sc else cookie
            return (json.loads(raw) if raw else {}), cookie_out
    except urllib.error.HTTPError as exc:
        # Treat deploy/restart gateways as retryable by callers
        if exc.code in {404, 502, 503, 504}:
            raise
        raw = exc.read().decode(errors="replace") if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code}: {raw[:300]}") from exc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild-queue", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--poll-seconds", type=int, default=45)
    ap.add_argument("--wait-deploy-hint", default="full-scale-production-funnel")
    args = ap.parse_args()

    email = (os.environ.get("APP_EMAIL") or "").strip()
    password = (os.environ.get("APP_PASSWORD") or "").strip()
    if not email or not password:
        print(json.dumps({"ok": False, "error": "APP_EMAIL/APP_PASSWORD required"}))
        return 2

    cookie = ""
    # Wait briefly for deploy containing our endpoints (bounded)
    deadline = time.time() + 900
    health = {}
    hint = str(args.wait_deploy_hint or "full-scale-production-funnel")
    while time.time() < deadline:
        try:
            health, _ = _req("", "GET", "/api/m3/health", timeout=30)
            # Login
            _, cookie = _req(
                "",
                "POST",
                "/api/login",
                {"email": email, "password": password},
                timeout=30,
            )
            # Probe scorecard endpoint (404 until deploy)
            try:
                sc, cookie = _req(cookie, "GET", "/api/m3/full-scale-production-funnel/scorecard", timeout=60)
                hb = str(health.get("build_version") or "")
                sb = str(sc.get("build") or sc.get("build_version") or "")
                ready = hint in hb or hint in sb or "full-scale-production-funnel" in sb
                if ready and (sc.get("metrics") is not None or sc.get("build")):
                    print(json.dumps({"deploy_ready": True, "health_build": hb, "scorecard_build": sb}))
                    break
                print(json.dumps({"wait_deploy": True, "health_build": hb, "scorecard_build": sb or None}))
            except urllib.error.HTTPError as exc:
                if exc.code not in {404, 502, 503, 504}:
                    raise
                print(json.dumps({"wait_deploy": True, "http": exc.code, "health_build": health.get("build_version")}))
        except Exception as exc:
            print(json.dumps({"wait": str(exc)[:160]}))
        time.sleep(12)
    else:
        print(json.dumps({"ok": False, "error": "deploy_timeout", "health": health}))
        return 1

    start = None
    for attempt in range(8):
        try:
            start, cookie = _req(
                cookie,
                "POST",
                "/api/m3/full-scale-production-funnel/start",
                {"force": bool(args.force), "rebuild_queue": bool(args.rebuild_queue)},
                timeout=60,
            )
            break
        except Exception as exc:
            print(json.dumps({"start_retry": attempt + 1, "error": str(exc)[:160]}))
            time.sleep(10)
    if not start:
        print(json.dumps({"ok": False, "error": "start_failed"}))
        return 1
    print(
        json.dumps(
            {
                "phase": "START",
                "status": start.get("status"),
                "job_id": start.get("job_id"),
                "worker_id": start.get("worker_id"),
                "eligible_total": start.get("eligible_total"),
                "slice_size": start.get("slice_size"),
                "build": start.get("build"),
                "build_version": start.get("build_version"),
                "AUTO_SEND": start.get("AUTO_SEND"),
            },
            indent=2,
            default=str,
        )
    )

    # Verify heartbeat advances (bounded — do not babysit full run)
    end = time.time() + max(15, int(args.poll_seconds))
    last = None
    snapshots = []
    while time.time() < end:
        time.sleep(8)
        prog, cookie = _req(cookie, "GET", "/api/m3/full-scale-production-funnel/progress", timeout=60)
        sc, cookie = _req(cookie, "GET", "/api/m3/full-scale-production-funnel/scorecard", timeout=60)
        p = prog.get("progress") or {}
        snap = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": p.get("status") or start.get("status"),
            "phase": p.get("phase"),
            "job_id": p.get("job_id") or start.get("job_id"),
            "worker_id": p.get("worker_id") or start.get("worker_id"),
            "heartbeat_at": p.get("heartbeat_at"),
            "completed": (p.get("completed") or (sc.get("metrics") or {}).get("opportunities_processed")),
            "eligible_total": p.get("eligible_total") or (sc.get("metrics") or {}).get("live_eligible_product_opportunities"),
            "current_opportunity_id": p.get("current_opportunity_id"),
            "metrics": sc.get("metrics"),
            "execution_state": "RUNNING" if str(p.get("status") or "").upper() in {"RUNNING", "QUEUED"} else p.get("status"),
        }
        snapshots.append(snap)
        print(json.dumps({"phase": "POLL", **snap}, default=str))
        last = snap
        if snap.get("status") in {"DONE", "ERROR", "CANCELLED", "BLOCKED"}:
            break

    out = {
        "ok": True,
        "FINAL_DIRECTIVE_STATE": (last or {}).get("execution_state") or "RUNNING",
        "NOTE": "Full queue continues on Railway independently of Cursor — not COMPLETE unless status=DONE",
        "start": {
            "status": start.get("status"),
            "job_id": start.get("job_id"),
            "worker_id": start.get("worker_id"),
            "eligible_total": start.get("eligible_total"),
        },
        "last": last,
        "ui": BASE + "/full_scale_funnel.html",
        "health_build": health.get("build_version"),
        "polls": len(snapshots),
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
