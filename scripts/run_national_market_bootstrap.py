"""Capture BEFORE discovery status, wait for new build, run NATIONAL bootstrap, capture AFTER."""
from __future__ import annotations

import json
import os
import time
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("GOVTRACKER_BASE_URL") or "https://samgov-production.up.railway.app"
OUT = ROOT / "artifacts" / "national_market_bootstrap_result.json"
EXPECTED_BUILD = "20260917-m3-discovery-intake-open"


def load_dotenv():
    for p in (ROOT / ".env", Path.cwd() / ".env"):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            key, val = k.strip(), v.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = val
        break


def main():
    load_dotenv()
    email = (os.environ.get("APP_EMAIL") or "").strip()
    password = (os.environ.get("APP_PASSWORD") or "").strip()
    if not email or not password:
        raise SystemExit("APP_EMAIL/APP_PASSWORD required")

    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def req(method: str, path: str, body: dict | None = None, timeout: int = 120):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
        with opener.open(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    login = req("POST", "/api/login", {"email": email, "password": password})
    print("login", login.get("ok"))

    # Wait for deploy of expected build (public health first)
    build = None
    for i in range(80):
        try:
            health = req("GET", "/api/health", timeout=30)
            build = health.get("build_version") or health.get("build")
        except Exception as exc:
            print("health wait", i, exc)
            time.sleep(15)
            continue
        print("deploy_poll", i, "build=", build)
        if build == EXPECTED_BUILD or (isinstance(build, str) and "discovery-intake" in build):
            break
        time.sleep(15)
    else:
        print("WARNING: expected build not seen; proceeding with current", build)

    before_status = req("GET", "/api/m3/discovery/status")
    before_runs = req("GET", "/api/m3/discovery/runs?limit=3")
    before = {
        "build": build,
        "status": before_status,
        "recent_runs": before_runs,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    print(
        "BEFORE",
        "running=",
        before_status.get("running"),
        "survivors=",
        (before_status.get("last_completed_run") or before_status.get("last_run") or {}).get(
            "product_screen_survivors"
        ),
    )

    # Wait if something already running
    for _ in range(120):
        st = req("GET", "/api/m3/discovery/status")
        if not st.get("running"):
            break
        print("waiting prior run", st.get("progress_percent"), st.get("phase"))
        time.sleep(30)

    trigger = req(
        "POST",
        "/api/m3/discovery/run",
        {"profile": "national", "bootstrap": True},
    )
    print(
        "trigger",
        trigger.get("accepted"),
        trigger.get("run_id"),
        trigger.get("profile"),
        trigger.get("bootstrap"),
        trigger.get("already_running"),
    )

    final = None
    for i in range(240):  # up to ~2h at 30s
        st = req("GET", "/api/m3/discovery/status")
        cur = st.get("current_run") or st.get("last_completed_run") or {}
        print(
            i,
            st.get("status"),
            "pct=",
            st.get("progress_percent") or cur.get("progress_percent"),
            "phase=",
            st.get("phase") or cur.get("phase"),
            "attempted=",
            cur.get("sources_attempted"),
            "ok=",
            cur.get("sources_successful"),
            "raw=",
            cur.get("records_retrieved"),
            "uniq=",
            cur.get("unique_records"),
            "surv=",
            cur.get("product_screen_survivors"),
        )
        if not st.get("running") and i > 0:
            final = st
            # ensure completion fields populated
            if (st.get("last_completed_run") or cur).get("run_id") or st.get("status") in {
                "COMPLETED",
                "COMPLETED_WITH_WARNINGS",
                "FAILED",
                "IDLE",
            }:
                break
        time.sleep(30)

    after_status = final or req("GET", "/api/m3/discovery/status")
    after_runs = req("GET", "/api/m3/discovery/runs?limit=5")
    last = (
        after_status.get("last_completed_run")
        or after_status.get("last_run")
        or (after_runs.get("runs") or [{}])[0]
    )

    result = {
        "expected_build": EXPECTED_BUILD,
        "observed_build": build,
        "before": before,
        "trigger": trigger,
        "after_status": after_status,
        "after_runs": after_runs,
        "last_run_summary": last,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("wrote", OUT)
    print(
        "AFTER summary attempted=",
        last.get("sources_attempted"),
        "successful=",
        last.get("sources_successful"),
        "raw=",
        last.get("records_retrieved") or last.get("raw_notices_seen"),
        "unique=",
        last.get("unique_records"),
        "survivors=",
        last.get("product_screen_survivors"),
        "metrics=",
        (last.get("metrics_json") or {}) if isinstance(last.get("metrics_json"), dict) else "n/a",
    )


if __name__ == "__main__":
    main()
