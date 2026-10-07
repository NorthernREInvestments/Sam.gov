"""Kick BidNet engine recovery on Railway and poll the completion report."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
TARGET = "20261006-m3-bidnet-engine-recovery-v1"


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def main() -> int:
    _load_dotenv()
    client = httpx.Client(base_url=BASE, timeout=120, follow_redirects=True)
    client.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    for i in range(90):
        try:
            env = client.get("/api/m3/bidnet-full-production/env-check").json()
            tl = env.get("thread_limits") or {}
            print(
                f"env {i}: {env.get('build_version')} recovery={env.get('recovery_walker')} "
                f"threads_ok={tl.get('verified_active')} browser={env.get('bidnet_browser_workers')} "
                f"logical={env.get('bidnet_logical_workers')}",
                flush=True,
            )
            if TARGET in str(env.get("build_version") or "") and int(env.get("recovery_walker") or 0) >= 1:
                break
        except Exception as exc:
            print(f"env {i}: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(10)
    else:
        print("build_timeout", flush=True)
        return 2

    kicked = client.post(
        "/api/m3/bidnet-engine/recovery/run",
        json={"stability_sample": 6, "resume_batch": 40},
    )
    print("kick", kicked.status_code, kicked.text[:500], flush=True)
    kicked.raise_for_status()
    job_id = kicked.json()["job_id"]

    # Brief polls only — never long-hang the API poller; also check durable progress/report.
    for i in range(240):
        try:
            health = client.get("/api/health", timeout=30)
            health_ok = health.status_code == 200
        except Exception:
            health_ok = False
        try:
            job = client.get(f"/api/m3/auth-jobs/{job_id}", timeout=30).json()
        except Exception as exc:
            print(f"poll {i}: job_fetch_err={type(exc).__name__} health_ok={health_ok}", flush=True)
            time.sleep(20)
            continue
        progress = job.get("progress") or {}
        result = job.get("result") or {}
        print(
            f"poll {i}: status={job.get('status')} phase={progress.get('phase')} pct={progress.get('pct')} "
            f"workers={progress.get('workers')} browsers={progress.get('browser_workers')} "
            f"completed={progress.get('completed')} health_ok={health_ok} "
            f"PASS={result.get('PASS_FAIL')} err={(job.get('error') or '')[:200]}",
            flush=True,
        )
        if job.get("status") in {"COMPLETED", "FAILED"}:
            report = client.get("/api/m3/bidnet-engine/recovery/report")
            payload = report.json() if report.status_code == 200 else {"job": job}
            out_dir = ROOT / "data"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "m3_bidnet_engine_recovery_v1_last_report.json").write_text(
                json.dumps(payload, indent=2, default=str),
                encoding="utf-8",
            )
            text = client.get("/api/m3/bidnet-engine/recovery/report?format=text")
            if text.status_code == 200:
                (out_dir / "m3_bidnet_engine_recovery_v1_last_report.txt").write_text(
                    text.text, encoding="utf-8"
                )
                print(text.text, flush=True)
            return 0 if job.get("status") == "COMPLETED" else 1
        time.sleep(20)
    print("poll_timeout", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
