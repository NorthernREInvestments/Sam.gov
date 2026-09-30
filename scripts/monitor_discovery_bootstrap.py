"""Monitor in-progress production discovery (no second trigger) and capture AFTER metrics."""
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

    req("POST", "/api/login", {"email": email, "password": password})
    health = req("GET", "/api/health")
    before = req("GET", "/api/m3/discovery/status")
    print("build", health.get("build_version"))
    print("BEFORE", json.dumps({k: before.get(k) for k in ("status", "running", "phase", "progress_percent")}, default=str))

    # If idle, trigger national bootstrap once
    trigger = None
    if not before.get("running"):
        trigger = req("POST", "/api/m3/discovery/run", {"profile": "national", "bootstrap": True})
        print("triggered", trigger.get("run_id"), trigger.get("profile"), trigger.get("accepted"))
    else:
        print("attaching to existing run", (before.get("current_run") or {}).get("run_id"))

    final = None
    for i in range(360):
        st = req("GET", "/api/m3/discovery/status")
        cur = st.get("current_run") or {}
        print(
            f"{i} status={st.get('status')} running={st.get('running')} "
            f"pct={st.get('progress_percent') or cur.get('progress_percent')} "
            f"phase={st.get('phase') or cur.get('phase')} "
            f"att={cur.get('sources_attempted')}/{cur.get('sources_total')} "
            f"ok={cur.get('sources_successful')} fail={cur.get('sources_failed')} "
            f"raw={cur.get('records_retrieved')} uniq={cur.get('unique_records')} "
            f"open={cur.get('open_current_records')} surv={cur.get('product_screen_survivors')} "
            f"queued={cur.get('deep_research_queued')}"
        )
        if not st.get("running") and i > 0:
            final = st
            break
        time.sleep(20)

    after = final or req("GET", "/api/m3/discovery/status")
    runs = req("GET", "/api/m3/discovery/runs?limit=5")
    last = after.get("last_completed_run") or after.get("last_run") or (runs.get("runs") or [{}])[0]
    pipe = None
    try:
        pipe = req("GET", "/api/health")
    except Exception:
        pass

    result = {
        "build": health.get("build_version"),
        "before": before,
        "trigger": trigger,
        "after": after,
        "runs": runs,
        "last_run": last,
        "pipeline_opportunity_count": (pipe or {}).get("pipeline_store", {}).get("opportunity_count")
        if isinstance(pipe, dict)
        else None,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT)
    print("LAST", json.dumps({
        "run_id": last.get("run_id"),
        "sources_attempted": last.get("sources_attempted"),
        "sources_successful": last.get("sources_successful"),
        "sources_failed": last.get("sources_failed"),
        "records_retrieved": last.get("records_retrieved") or last.get("raw_notices_seen"),
        "unique_records": last.get("unique_records"),
        "product_screen_survivors": last.get("product_screen_survivors"),
        "metrics_json": last.get("metrics_json"),
        "notes": last.get("notes"),
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
