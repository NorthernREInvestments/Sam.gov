"""Kick the BidNet downstream census on Railway and poll until the report is written."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
TARGET = "20261006-m3-bidnet-downstream-processing-v1"


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
                f"env {i}: {env.get('build_version')} downstream={env.get('downstream_walker')}",
                flush=True,
            )
            if TARGET in str(env.get("build_version") or "") and int(env.get("downstream_walker") or 0) >= 3:
                break
        except Exception as exc:
            print(f"env {i}: {type(exc).__name__}", flush=True)
        time.sleep(10)
    else:
        print("build_timeout", flush=True)
        return 2

    kicked = client.post("/api/m3/bidnet-downstream/run")
    print("kick", kicked.status_code, kicked.text[:400], flush=True)
    kicked.raise_for_status()
    job_id = kicked.json()["job_id"]
    for i in range(180):
        job = client.get(f"/api/m3/auth-jobs/{job_id}").json()
        progress = job.get("progress") or {}
        result = job.get("result") or {}
        print(
            f"poll {i}: status={job.get('status')} phase={progress.get('phase')} pct={progress.get('pct')} "
            f"completed={progress.get('completed')} total={progress.get('total')} "
            f"PASS={result.get('PASS_FAIL')} input={result.get('input_valid_open')} "
            f"err={(job.get('error') or '')[:200]}",
            flush=True,
        )
        if job.get("status") in {"COMPLETED", "FAILED"}:
            report = client.get("/api/m3/bidnet-downstream/report")
            payload = report.json() if report.status_code == 200 else {"job": job}
            out = ROOT / "data" / "m3_bidnet_downstream_v1_last_report.json"
            out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            text = client.get("/api/m3/bidnet-downstream/report?format=text")
            if text.status_code == 200:
                (ROOT / "data" / "m3_bidnet_downstream_v1_last_report.txt").write_text(text.text, encoding="utf-8")
                print(text.text, flush=True)
            ok = job.get("status") == "COMPLETED"
            return 0 if ok else 1
        time.sleep(15)
    print("poll_timeout", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
