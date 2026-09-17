"""One-shot production discovery trigger + status capture (public data only)."""
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
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "artifacts" / "m3_initial_production_discovery_run.json"


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
        os.environ[key] = val


def main():
    load_dotenv()
    email = os.environ.get("APP_EMAIL", "").strip()
    password = os.environ.get("APP_PASSWORD", "").strip()
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
        r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        with opener.open(r, timeout=120) as resp:
            return json.loads(resp.read().decode())

    login = req("POST", "/api/login", {"email": email, "password": password})
    print("login", login.get("ok") or login)

    status0 = req("GET", "/api/m3/discovery/status")
    print("status0", status0.get("status"), status0.get("running"), status0.get("progress_percent"))

    trigger = None
    if status0.get("running"):
        trigger = {"accepted": False, "already_running": True, "message": "Discovery already running", "status": status0}
        print("already running — attaching to existing run")
    else:
        trigger = req("POST", "/api/m3/discovery/run")
        print("trigger", trigger.get("accepted"), trigger.get("already_running"), trigger.get("run_id"))

    final = None
    started = time.time()
    for i in range(90):
        st = req("GET", "/api/m3/discovery/status")
        cur = st.get("current_run") or {}
        print(
            i,
            st.get("status"),
            st.get("progress_percent"),
            cur.get("phase"),
            cur.get("sources_completed"),
            "/",
            cur.get("sources_total"),
            "survivors",
            cur.get("product_screen_survivors"),
        )
        if not st.get("running"):
            final = st
            # Prefer last_attempt if just finished
            break
        time.sleep(10)
    else:
        final = req("GET", "/api/m3/discovery/status")

    dash = req("GET", "/api/m3/mobile/dashboard")
    health = req("GET", "/api/m3/health")
    last = (final or {}).get("last_successful_completion") or (final or {}).get("last_attempt") or {}
    report = {
        "kind": "M3InitialProductionDiscoveryRun",
        "base": BASE,
        "elapsed_poll_seconds": round(time.time() - started, 1),
        "trigger": {
            "accepted": trigger.get("accepted"),
            "already_running": trigger.get("already_running"),
            "run_id": trigger.get("run_id"),
            "message": trigger.get("message"),
        },
        "status_before": {
            "status": status0.get("status"),
            "running": status0.get("running"),
            "last_successful_completion": status0.get("last_successful_completion"),
        },
        "final_status": final,
        "run_metrics": {
            "started_at": last.get("started_at"),
            "completed_at": last.get("completed_at"),
            "status": last.get("status"),
            "trigger_type": last.get("trigger_type"),
            "sources_attempted": last.get("sources_attempted"),
            "sources_successful": last.get("sources_successful"),
            "sources_failed": last.get("sources_failed"),
            "records_retrieved": last.get("records_retrieved"),
            "unique_records": last.get("unique_records"),
            "open_current_records": last.get("open_current_records"),
            "product_screen_survivors": last.get("product_screen_survivors"),
            "pipeline_new": last.get("pipeline_new"),
            "pipeline_updated": last.get("pipeline_updated"),
            "pipeline_rejected": last.get("pipeline_rejected"),
            "deep_research_queued": last.get("deep_research_queued"),
            "error_summary": last.get("error_summary"),
            "source_warnings": last.get("source_warnings"),
        },
        "home_counts": {
            "active_count": dash.get("active_count"),
            "action_count": dash.get("action_count"),
            "opportunities_shown": len(dash.get("active_opportunities") or []),
        },
        "health": {
            "build_version": health.get("build_version"),
            "pipeline_opportunity_count": health.get("pipeline_opportunity_count"),
            "DEVELOPMENT_NO_OUTREACH": health.get("DEVELOPMENT_NO_OUTREACH"),
        },
        "commercial_outreach": False,
        "real_external_spend_usd": 0,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("WROTE", OUT)
    print(json.dumps(report["run_metrics"], indent=2))
    print(json.dumps(report["home_counts"], indent=2))


if __name__ == "__main__":
    main()
