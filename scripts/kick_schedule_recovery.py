"""Kick same-13 authoritative schedule recovery — no long polling."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = "20261007-m3-authoritative-schedule-recovery-v1"
BASE = "https://samgov-production.up.railway.app"


def _get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    iteration = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    change = sys.argv[2] if len(sys.argv) > 2 else "content-first schedule recognition"
    for i in range(24):
        try:
            env = _get("/api/m3/env")
        except Exception as exc:
            print(f"wait {i}: env error {exc}")
            time.sleep(10)
            continue
        print(
            f"wait {i}: build={env.get('build_version')} sr={env.get('schedule_recovery_walker')}",
        )
        if TARGET in str(env.get("build_version") or "") and int(env.get("schedule_recovery_walker") or 0) >= 1:
            break
        time.sleep(10)
    else:
        print("BUILD_NOT_LIVE")
        return 2

    job = _post(
        "/api/m3/schedule-recovery/run",
        {
            "mode": "same_13",
            "canary_n": 20,
            "price_budget": 25,
            "iteration": iteration,
            "change_made": change,
        },
    )
    print(json.dumps(job, indent=2))
    (ROOT / "data" / "m3_schedule_recovery_kick.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
