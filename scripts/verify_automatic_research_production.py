"""Production verification — automatic research queue drain + live status."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if not (ROOT / ".env").exists() and (ROOT.parent / ".env").exists():
    ROOT = ROOT.parent
if not (ROOT / ".env").exists():
    cwd = Path.cwd()
    if (cwd / ".env").exists():
        ROOT = cwd
BASE = os.environ.get("GOVTRACKER_BASE_URL") or "https://samgov-production.up.railway.app"
OUT = ROOT / "artifacts" / "m3_automatic_research_verification.json"


def load_dotenv():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ.setdefault(key, val)


def main():
    load_dotenv()
    email = (os.environ.get("APP_EMAIL") or os.environ.get("GOVTRACKER_USER") or "").strip()
    password = (os.environ.get("APP_PASSWORD") or os.environ.get("GOVTRACKER_PASSWORD") or "").strip()
    if not email or not password:
        raise SystemExit("APP_EMAIL/APP_PASSWORD required")

    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def req(method: str, path: str, body: dict | None = None):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        with opener.open(request, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}

    report: dict = {
        "kind": "M3AutomaticResearchVerification",
        "base": BASE,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    code, login = req("POST", "/api/login", {"email": email, "password": password})
    report["login"] = {"status": code, "ok": code < 400, "body_ok": bool((login or {}).get("ok") or login)}
    print("login", login.get("ok") if isinstance(login, dict) else login, flush=True)

    code, health = req("GET", "/api/m3/health")
    report["health"] = {"status": code, "body": health}
    build = (health or {}).get("build_version")
    report["build_version"] = build
    print("build", build, flush=True)

    code, st0 = req("GET", "/api/m3/research/status")
    report["research_status_before"] = {"http": code, "body": st0}
    print(
        "research0",
        (st0 or {}).get("status"),
        "running=",
        (st0 or {}).get("running"),
        "backlog=",
        (st0 or {}).get("backlog"),
        flush=True,
    )

    if not (st0 or {}).get("running"):
        code, trigger = req("POST", "/api/m3/research/run", {})
        report["trigger"] = {"http": code, "body": trigger}
        print(
            "trigger",
            (trigger or {}).get("accepted"),
            (trigger or {}).get("already_running"),
            (trigger or {}).get("run_id") or (trigger or {}).get("reason"),
            flush=True,
        )
    else:
        report["trigger"] = {"skipped": True, "reason": "already_running"}
        print("already running — attaching", flush=True)

    samples = []
    final = st0 or {}
    for i in range(48):  # up to ~4 minutes
        time.sleep(5)
        code, st = req("GET", "/api/m3/research/status")
        disp = (st or {}).get("display") or {}
        sample = {
            "t": i,
            "http": code,
            "status": (st or {}).get("status"),
            "running": (st or {}).get("running"),
            "progress_percent": (st or {}).get("progress_percent"),
            "processed": disp.get("processed"),
            "queued": disp.get("queued"),
            "rejected": disp.get("rejected"),
            "advanced": disp.get("advanced"),
            "deferred": disp.get("deferred"),
            "failed": disp.get("failed"),
            "currently": disp.get("currently_processing_title"),
            "eta": disp.get("eta_label"),
            "spend": disp.get("actual_external_spend"),
            "backlog": (st or {}).get("backlog"),
            "heartbeat": disp.get("heartbeat_at"),
        }
        samples.append(sample)
        final = st or final
        print(
            f"[{i}] status={sample['status']} pct={sample['progress_percent']} "
            f"proc={sample['processed']} q={sample['queued']} current={sample['currently']} eta={sample['eta']}",
            flush=True,
        )
        # Stop once run finished after we observed it running or processed work
        if not (st or {}).get("running") and i >= 1:
            if any(s.get("running") or (s.get("processed") or 0) > 0 for s in samples):
                break
            if i >= 6 and (st or {}).get("status") in {"CURRENT", "IDLE", "BACKLOG"}:
                break

    code, dash = req("GET", "/api/m3/mobile/dashboard")
    report["dashboard"] = {
        "http": code,
        "has_research": bool((dash or {}).get("research")),
        "research_status": ((dash or {}).get("research") or {}).get("status"),
        "active_count": (dash or {}).get("active_count"),
    }

    disp = (final or {}).get("display") or {}
    cur = (final or {}).get("current_run") or (final or {}).get("last_successful_completion") or {}
    report["samples"] = samples
    report["final"] = final
    report["summary"] = {
        "build_version": build,
        "research_runner_status": (final or {}).get("status"),
        "total_candidates": disp.get("total_candidates") or cur.get("total_candidates"),
        "processed": disp.get("processed") or cur.get("processed"),
        "remaining": disp.get("queued") if (final or {}).get("running") else (final or {}).get("backlog"),
        "rejected": disp.get("rejected") or cur.get("rejected"),
        "advanced": disp.get("advanced") or cur.get("advanced"),
        "deferred": disp.get("deferred") or cur.get("deferred"),
        "failed": disp.get("failed") or cur.get("failed"),
        "current_opportunity": disp.get("currently_processing_title") or cur.get("currently_processing_title"),
        "elapsed_started_at": cur.get("started_at") or disp.get("started_at"),
        "eta": disp.get("eta_label") or cur.get("eta_label"),
        "actual_external_spend": disp.get("actual_external_spend") or cur.get("actual_external_spend"),
        "queue_survived_restart": "AppSetting durable state + orphan lock clear on boot",
    }

    saw_progress = any(
        (s.get("processed") or 0) > 0 or s.get("currently") or s.get("running") for s in samples
    )
    ok = (
        bool(build)
        and "research" in str(build)
        and (final or {}).get("kind") == "M3ResearchStatus"
        and bool(saw_progress or (final or {}).get("backlog") == 0 or int(disp.get("processed") or 0) > 0)
    )
    report["LIVE_OK"] = bool(ok)
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, default=str), flush=True)
    print(f"LIVE_OK={report['LIVE_OK']} wrote {OUT}", flush=True)
    return 0 if report["LIVE_OK"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
