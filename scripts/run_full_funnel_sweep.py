"""Kick + poll full funnel sweep job on production. Heartbeat polls; job runs server-side."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "FULL_FUNNEL_SWEEP_REPORT.json"
POLL_SEC = 30
MAX_POLLS = 480  # ~4h observe window; job continues server-side up to FFS stale


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
    if health.get("build_version") != "20261003-m3-full-funnel-sweep-v1":
        print("WARN: expected build 20261003-m3-full-funnel-sweep-v1", flush=True)

    # Skip rediscovery by default if prior E2E just finished; still do free-package universe.
    # Pass skip_discovery=false via env FULL_SWEEP_REDISCOVER=1 to re-harvest.
    rediscover = os.environ.get("FULL_SWEEP_REDISCOVER", "").strip() in {"1", "true", "yes"}
    resume = os.environ.get("FFS_RESUME", "1").strip() not in {"0", "false", "no"}
    body = {
        "skip_discovery": not rediscover,
        "skip_bidnet": not rediscover,
        "skip_opengov": not rediscover,
        "skip_expansion": not rediscover,
        "free_package_batch_size": int(os.environ.get("FFS_BATCH", "5000")),
        "free_package_max_batches": int(os.environ.get("FFS_MAX_BATCHES", "20")),
        "opengov_recovery_limit": None,
        "resume": resume,
    }
    r = c.post("/api/m3/full-funnel-sweep/run", json=body, timeout=60)
    print("start", r.status_code, r.text[:500], flush=True)
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
            f"ret={p.get('retrieved')} found={p.get('found')} batch={p.get('batch')} "
            f"remain={p.get('remaining')}",
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
        "result": last.get("result"),
        "error": last.get("error"),
        "progress": last.get("progress"),
    }
    try:
        disk = c.get("/api/m3/full-funnel-sweep/last-report", timeout=90).json()
        if disk and disk.get("kind"):
            report["disk_report"] = disk
    except Exception as exc:
        report["disk_report_error"] = type(exc).__name__

    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    summary = ((last.get("result") or {}).get("owner_summary")) if isinstance(last.get("result"), dict) else None
    if summary:
        print(summary, flush=True)
    else:
        print(json.dumps(report.get("result") or report, indent=2, default=str)[:8000], flush=True)
    print("wrote", OUT, flush=True)
    return 0 if last.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
