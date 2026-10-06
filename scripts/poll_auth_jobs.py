"""Start async auth jobs ONE AT A TIME and poll with short bounded waits."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
POLL_SEC = 15
MAX_POLLS = 16  # ~4 minutes max per job poll window (execution-time rule)
OUT = ROOT / "data" / "production_harden_async_report.json"


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


def poll(client: httpx.Client, job_id: str, label: str) -> dict:
    print(f"poll_start {label} {job_id}", flush=True)
    last: dict = {}
    for i in range(MAX_POLLS):
        r = client.get(f"/api/m3/auth-jobs/{job_id}", timeout=30)
        if r.status_code != 200:
            print(f"poll {i}: http {r.status_code}", flush=True)
            time.sleep(POLL_SEC)
            continue
        data = r.json()
        st = data.get("status")
        prog = data.get("progress") or {}
        print(
            f"poll {i}: status={st} phase={prog.get('phase')} pct={prog.get('pct')}",
            flush=True,
        )
        last = data
        if st in {"COMPLETED", "FAILED"}:
            return data
        time.sleep(POLL_SEC)
    last["poll_timeout"] = True
    print(f"poll_timeout {label} {job_id} after {MAX_POLLS * POLL_SEC}s", flush=True)
    return last


def main() -> int:
    _load_dotenv()
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    print("login_ok", flush=True)
    report: dict = {"build": None}

    try:
        report["build"] = c.get("/api/health", timeout=30).json().get("build_version")
    except Exception:
        pass

    # Stage A: BidNet list sample (async) — single-flight; do not overlap
    r = c.post(
        "/api/m3/bidnet-discovery/harvest",
        json={
            "max_results": 100,
            "max_pages": 8,
            "open_details": False,
            "persist": True,
            "async": True,
        },
    )
    print("start bn100", r.status_code, r.text[:300], flush=True)
    bn_start = r.json()
    job_a = bn_start.get("job_id")
    report["bidnet_100"] = poll(c, job_a, "bn100") if job_a else bn_start

    # Stage B: BidNet detail sample — only after A finishes or times out
    r = c.post(
        "/api/m3/bidnet-discovery/harvest",
        json={
            "max_results": 20,
            "max_pages": 2,
            "open_details": True,
            "detail_limit": 20,
            "persist": True,
            "async": True,
        },
    )
    print("start bn_detail20", r.status_code, r.text[:300], flush=True)
    bn_d = r.json()
    job_b = bn_d.get("job_id")
    report["bidnet_detail20"] = poll(c, job_b, "bn_detail20") if job_b else bn_d

    # Stage C: OpenGov discovery sample
    r = c.post(
        "/api/m3/opengov-discovery/run",
        json={"max_entities": 5, "max_pages": 2, "persist": True, "async": True},
    )
    print("start opengov", r.status_code, r.text[:300], flush=True)
    og_start = r.json()
    job_c = og_start.get("job_id")
    report["opengov"] = poll(c, job_c, "opengov") if job_c else og_start

    report["bidnet_status"] = c.get("/api/m3/bidnet-auth/status").json()
    report["opengov_status"] = c.get("/api/m3/opengov-auth/status").json()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT, flush=True)
    print(
        json.dumps(
            {
                "build": report.get("build"),
                "bn100": (report.get("bidnet_100") or {}).get("status"),
                "bn_detail": (report.get("bidnet_detail20") or {}).get("status"),
                "opengov": (report.get("opengov") or {}).get("status"),
                "bn_harvested": (report.get("bidnet_status") or {}).get("harvested"),
                "bn_pct": (report.get("bidnet_status") or {}).get("pagination_pct"),
                "og_raw": (report.get("opengov_status") or {}).get("raw_opportunities"),
            },
            default=str,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
