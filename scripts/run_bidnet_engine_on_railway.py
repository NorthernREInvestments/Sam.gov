"""Kick BidNet incremental/parallel engine on Railway and poll the report."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
TARGET = "20261006-m3-bidnet-incremental-parallel-engine-v1"


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
    for i in range(60):
        try:
            env = client.get("/api/m3/bidnet-full-production/env-check").json()
            print(
                f"env {i}: {env.get('build_version')} engine={env.get('engine_walker')}",
                flush=True,
            )
            if TARGET in str(env.get("build_version") or "") and int(env.get("engine_walker") or 0) >= 1:
                break
        except Exception as exc:
            print(f"env {i}: {type(exc).__name__}", flush=True)
        time.sleep(10)
    else:
        print("build_timeout", flush=True)
        return 2

    kicked = client.post(
        "/api/m3/bidnet-engine/run",
        json={
            "scale_sample_size": 8,
            "baseline_batch": 60,
            "stress_n": 5000,
            "time_budget_s": 2400,
        },
    )
    print("kick", kicked.status_code, kicked.text[:500], flush=True)
    kicked.raise_for_status()
    job_id = kicked.json()["job_id"]
    for i in range(400):
        job = client.get(f"/api/m3/auth-jobs/{job_id}").json()
        progress = job.get("progress") or {}
        result = job.get("result") or {}
        after = result.get("after") or {}
        print(
            f"poll {i}: status={job.get('status')} phase={progress.get('phase')} pct={progress.get('pct')} "
            f"workers={progress.get('workers') or result.get('selected_workers')} "
            f"completed={progress.get('completed')} rate={progress.get('rate') or after.get('throughput_per_min')} "
            f"PASS={result.get('PASS_FAIL')} err={(job.get('error') or '')[:200]}",
            flush=True,
        )
        if job.get("status") in {"COMPLETED", "FAILED"}:
            report = client.get("/api/m3/bidnet-engine/report")
            payload = report.json() if report.status_code == 200 else {"job": job}
            (ROOT / "data" / "m3_bidnet_engine_v1_last_report.json").write_text(
                json.dumps(payload, indent=2, default=str),
                encoding="utf-8",
            )
            text = client.get("/api/m3/bidnet-engine/report?format=text")
            if text.status_code == 200:
                (ROOT / "data" / "m3_bidnet_engine_v1_last_report.txt").write_text(text.text, encoding="utf-8")
                print(text.text, flush=True)
            return 0 if job.get("status") == "COMPLETED" else 1
        time.sleep(15)
    print("poll_timeout", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
