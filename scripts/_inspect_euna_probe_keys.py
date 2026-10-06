from __future__ import annotations

import json
import os
import time
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


def main() -> int:
    _load_dotenv()
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    print("build", c.get("/api/health").json().get("build_version"), flush=True)
    r = c.post("/api/m3/euna-discovery/api-probe", json={})
    jid = r.json().get("job_id")
    print("job", jid, flush=True)
    for i in range(24):
        d = c.get(f"/api/m3/auth-jobs/{jid}", timeout=30).json()
        print(f"poll {i}: {d.get('status')}", flush=True)
        if d.get("status") in {"COMPLETED", "FAILED"}:
            res = d.get("result") or {}
            print("keys", sorted(res.keys()), flush=True)
            print("account_entitlement", res.get("account_entitlement"), flush=True)
            print("vendor_flags", res.get("vendor_flags"), flush=True)
            print("feature_flags_status", res.get("feature_flags_status"), flush=True)
            print("feature_flags_summary", json.dumps(res.get("feature_flags_summary"), default=str)[:1500], flush=True)
            print("api_path_probe", json.dumps(res.get("api_path_probe"), default=str)[:2000], flush=True)
            print("nav_clicks", json.dumps(res.get("nav_clicks"), default=str)[:1200], flush=True)
            print("final_text_sample", (res.get("final_text_sample") or "")[:400], flush=True)
            (ROOT / "data" / "euna_api_probe_result.json").write_text(
                json.dumps(d, indent=2, default=str), encoding="utf-8"
            )
            return 0 if d.get("status") == "COMPLETED" else 1
        time.sleep(10)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
