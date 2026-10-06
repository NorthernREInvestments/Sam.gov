"""Staged async 3-source validation — bounded polls (execution-time rule)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "three_source_validation_report.json"
POLL_SEC = 15
MAX_POLLS = 20  # ~5 min per job poll window


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


def poll(c: httpx.Client, job_id: str, label: str, max_polls: int = MAX_POLLS) -> dict:
    print(f"poll_start {label} {job_id}", flush=True)
    last: dict = {}
    for i in range(max_polls):
        d = c.get(f"/api/m3/auth-jobs/{job_id}", timeout=30).json()
        prog = d.get("progress") or {}
        print(
            f"poll {i}: status={d.get('status')} phase={prog.get('phase')} "
            f"pct={prog.get('pct')} retrieved={prog.get('retrieved')} entities={prog.get('entities')}",
            flush=True,
        )
        last = d
        if d.get("status") in {"COMPLETED", "FAILED"}:
            return d
        time.sleep(POLL_SEC)
    last["poll_timeout"] = True
    return last


def main() -> int:
    _load_dotenv()
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    report: dict = {"build": c.get("/api/health", timeout=30).json().get("build_version")}

    # Stage A: BidNet full list (async) — do not open details
    r = c.post(
        "/api/m3/bidnet-discovery/harvest",
        json={
            "max_results": 25000,
            "max_pages": 1000,
            "open_details": False,
            "persist": True,
            "async": True,
        },
    )
    print("start bidnet_full", r.status_code, r.text[:300], flush=True)
    jid = r.json().get("job_id")
    # First poll window only — full 23k may exceed; report progress/checkpoint
    report["bidnet_full"] = poll(c, jid, "bidnet_full", max_polls=20) if jid else r.json()
    report["bidnet_job_id"] = jid

    # Stage B: OpenGov public-first (50 entities)
    r = c.post(
        "/api/m3/opengov-discovery/run",
        json={"max_entities": 50, "max_pages": 4, "persist": True, "async": True},
    )
    print("start opengov50", r.status_code, r.text[:300], flush=True)
    jid = r.json().get("job_id")
    report["opengov_50"] = poll(c, jid, "opengov50", max_polls=20) if jid else r.json()

    # Stage C: Euna/Bonfire
    r = c.post(
        "/api/m3/euna-discovery/run",
        json={"max_entities": 20, "max_pages": 4, "persist": True, "async": True},
    )
    print("start euna", r.status_code, r.text[:300], flush=True)
    jid = r.json().get("job_id")
    report["euna"] = poll(c, jid, "euna", max_polls=16) if jid else r.json()

    report["coverage"] = c.get("/api/m3/source-coverage/production", timeout=60).json()
    report["bidnet_status"] = c.get("/api/m3/bidnet-auth/status").json()
    report["opengov_status"] = c.get("/api/m3/opengov-auth/status").json()
    report["euna_status"] = c.get("/api/m3/euna-auth/status").json()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT, flush=True)
    print(
        "SUMMARY",
        json.dumps(
            {
                "build": report.get("build"),
                "bn": (report.get("bidnet_full") or {}).get("status"),
                "og": (report.get("opengov_50") or {}).get("status"),
                "eu": (report.get("euna") or {}).get("status"),
                "bn_h": (report.get("bidnet_status") or {}).get("harvested"),
                "og_raw": (report.get("opengov_status") or {}).get("raw_opportunities"),
                "eu_h": (report.get("euna_status") or {}).get("harvested"),
            },
            default=str,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
