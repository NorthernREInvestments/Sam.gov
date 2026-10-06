"""Kick + poll full production E2E job. Bounded polls with progress heartbeat."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "FULL_PRODUCTION_E2E_REPORT.json"
POLL_SEC = 20
MAX_POLLS = 270  # ~90 min observable window; job itself may run longer server-side


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
    c = httpx.Client(base_url=BASE, timeout=90, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    health = c.get("/api/health", timeout=30).json()
    print("build", health.get("build_version"), flush=True)
    before = c.get("/api/m3/source-coverage/production", timeout=60).json()
    print(
        "canonical_before",
        (before.get("combined") or {}).get("canonical_live"),
        flush=True,
    )

    r = c.post(
        "/api/m3/full-production-e2e/run",
        json={
            "bidnet_max_results": 25000,
            "opengov_max_pages": 40,
            "recovery_bidnet_limit": 400,
            "recovery_opengov_limit": 200,
            "profit_limit": None,
            "skip_expansion": False,
        },
        timeout=60,
    )
    print("start", r.status_code, r.text[:400], flush=True)
    jid = (r.json() or {}).get("job_id")
    if not jid:
        OUT.write_text(json.dumps({"error": "no_job", "body": r.text[:1000]}, indent=2), encoding="utf-8")
        return 1

    last: dict = {}
    for i in range(MAX_POLLS):
        d = c.get(f"/api/m3/auth-jobs/{jid}", timeout=45).json()
        p = d.get("progress") or {}
        print(
            f"poll {i}: status={d.get('status')} phase={p.get('phase')} pct={p.get('pct')} "
            f"ret={p.get('retrieved')} ent={p.get('entities')} part={p.get('partition')}",
            flush=True,
        )
        last = d
        if d.get("status") in {"COMPLETED", "FAILED"}:
            break
        time.sleep(POLL_SEC)

    report = {
        "build": health.get("build_version"),
        "job_id": jid,
        "job_status": last.get("status"),
        "canonical_before_coverage": (before.get("combined") or {}),
        "result": last.get("result"),
        "error": last.get("error"),
        "progress": last.get("progress"),
    }
    # Prefer disk report if available
    try:
        disk = c.get("/api/m3/full-production-e2e/last-report", timeout=60).json()
        if disk and disk.get("kind"):
            report["disk_report"] = disk
    except Exception as exc:
        report["disk_report_error"] = type(exc).__name__

    try:
        report["coverage_after"] = c.get("/api/m3/source-coverage/production", timeout=60).json()
    except Exception:
        pass
    try:
        report["universe_funnel"] = c.get("/api/m3/universe-pass/funnel", timeout=90).json()
    except Exception as exc:
        report["universe_funnel_error"] = type(exc).__name__

    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT, flush=True)
    fu = ((report.get("disk_report") or report.get("result") or {}).get("full_universe") or {})
    print(
        "SUMMARY canonical_before=",
        fu.get("canonical_before"),
        "after=",
        fu.get("canonical_after"),
        "net_new=",
        fu.get("net_new"),
        flush=True,
    )
    return 0 if last.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
