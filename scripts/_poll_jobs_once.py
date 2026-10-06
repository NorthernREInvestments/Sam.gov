"""One-shot poll of known auth jobs + portal status (bounded)."""
from __future__ import annotations

import json
import os
import sys
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


def _compact_result(res: dict) -> dict:
    out: dict = {}
    h = res.get("harvest") or {}
    if h:
        out["harvest"] = {
            k: h.get(k)
            for k in (
                "retrieved_total",
                "pages_scanned",
                "reported_total",
                "DISCOVERY_TRUNCATED",
                "pagination_complete",
                "pagination_method",
                "truncation_reason",
                "fallback_url",
                "detail_stats",
                "detail_failure_counts",
            )
        }
    if res.get("canonical_merge"):
        m = res["canonical_merge"]
        out["merge"] = {
            k: m.get(k)
            for k in ("raw", "new", "updated", "duplicates_detected", "available_after")
        }
    for k in (
        "auth",
        "entities_successful",
        "entities_failed",
        "raw_opportunities",
        "truncated",
        "session_state",
        "status",
        "blocker",
    ):
        if k in res:
            out[k] = res[k]
    return out


def main() -> int:
    _load_dotenv()
    jobs = sys.argv[1:] or [
        "BNH-997181da8736",
        "BNH-935a8993043c",
    ]
    c = httpx.Client(base_url=BASE, timeout=45, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    print("login_ok", flush=True)

    for jid in jobs:
        r = c.get(f"/api/m3/auth-jobs/{jid}", timeout=30)
        print(f"JOB {jid} http={r.status_code}", flush=True)
        if r.status_code != 200:
            print(r.text[:400], flush=True)
            continue
        d = r.json()
        print(
            json.dumps(
                {
                    k: d.get(k)
                    for k in (
                        "job_id",
                        "kind",
                        "status",
                        "started_at",
                        "updated_at",
                        "completed_at",
                        "error",
                        "progress",
                        "params",
                    )
                },
                default=str,
            ),
            flush=True,
        )
        if d.get("result"):
            print("RESULT", json.dumps(_compact_result(d["result"]), default=str)[:3000], flush=True)

    for path in (
        "/api/m3/bidnet-auth/status",
        "/api/m3/opengov-auth/status",
        "/api/m3/auth-jobs/latest",
    ):
        r = c.get(path, timeout=30)
        print(f"PATH {path} http={r.status_code}", flush=True)
        if r.status_code == 200:
            print(json.dumps(r.json(), default=str)[:2500], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
