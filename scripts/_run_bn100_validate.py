"""Start one BidNet list harvest job and poll (~4 min max)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"


def _load_dotenv() -> None:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
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
    r = c.post(
        "/api/m3/bidnet-discovery/harvest",
        json={
            "max_results": 100,
            "max_pages": 6,
            "open_details": False,
            "persist": True,
            "async": True,
        },
    )
    print("start", r.status_code, r.text[:400], flush=True)
    job_id = r.json().get("job_id")
    last = {}
    for i in range(16):
        d = c.get(f"/api/m3/auth-jobs/{job_id}", timeout=30).json()
        prog = d.get("progress") or {}
        print(
            f"poll {i}: status={d.get('status')} phase={prog.get('phase')} "
            f"pct={prog.get('pct')} pages={prog.get('pages')} retrieved={prog.get('retrieved')}",
            flush=True,
        )
        last = d
        if d.get("status") in {"COMPLETED", "FAILED"}:
            break
        time.sleep(15)
    h = ((last.get("result") or {}).get("harvest")) or {}
    print(
        "FINAL",
        json.dumps(
            {
                "status": last.get("status"),
                "error": last.get("error"),
                "retrieved": h.get("retrieved_total"),
                "pages": h.get("pages_scanned"),
                "reported": h.get("reported_total"),
                "complete": h.get("pagination_complete"),
                "truncated": h.get("DISCOVERY_TRUNCATED"),
                "method": h.get("pagination_method"),
                "truncation_reason": h.get("truncation_reason"),
                "fallback": h.get("fallback_url"),
            },
            default=str,
        ),
        flush=True,
    )
    st = c.get("/api/m3/bidnet-auth/status").json()
    print(
        "STATUS",
        json.dumps(
            {
                k: st.get(k)
                for k in (
                    "status",
                    "harvested",
                    "reported_open",
                    "pagination_pct",
                    "pagination_complete",
                    "discovery_truncated",
                    "details_recovered",
                    "documents_recovered",
                )
            },
            default=str,
        ),
        flush=True,
    )
    return 0 if last.get("status") == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
