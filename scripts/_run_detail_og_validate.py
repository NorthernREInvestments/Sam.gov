"""Sequential BidNet detail20 + OpenGov discovery validation (bounded polls)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "production_harden_async_report.json"


def _load_dotenv() -> None:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k and k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")


def poll(c: httpx.Client, job_id: str, label: str, max_polls: int = 16) -> dict:
    print(f"poll_start {label} {job_id}", flush=True)
    last: dict = {}
    for i in range(max_polls):
        d = c.get(f"/api/m3/auth-jobs/{job_id}", timeout=30).json()
        prog = d.get("progress") or {}
        print(
            f"poll {i}: status={d.get('status')} phase={prog.get('phase')} "
            f"pct={prog.get('pct')} pages={prog.get('pages')} retrieved={prog.get('retrieved')}",
            flush=True,
        )
        last = d
        if d.get("status") in {"COMPLETED", "FAILED"}:
            return d
        time.sleep(15)
    last["poll_timeout"] = True
    return last


def main() -> int:
    _load_dotenv()
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    report: dict = {
        "build": c.get("/api/health", timeout=30).json().get("build_version"),
    }

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
    print("start detail", r.status_code, r.text[:300], flush=True)
    jid = r.json().get("job_id")
    report["bidnet_detail20"] = poll(c, jid, "detail20", max_polls=20) if jid else r.json()

    r = c.post(
        "/api/m3/opengov-discovery/run",
        json={"max_entities": 5, "max_pages": 2, "persist": True, "async": True},
    )
    print("start opengov", r.status_code, r.text[:300], flush=True)
    jid = r.json().get("job_id")
    report["opengov"] = poll(c, jid, "opengov", max_polls=16) if jid else r.json()

    report["bidnet_status"] = c.get("/api/m3/bidnet-auth/status").json()
    report["opengov_status"] = c.get("/api/m3/opengov-auth/status").json()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT, flush=True)

    det = report.get("bidnet_detail20") or {}
    dh = ((det.get("result") or {}).get("harvest")) or {}
    ds = dh.get("detail_stats") or {}
    og = report.get("opengov") or {}
    print(
        "SUMMARY",
        json.dumps(
            {
                "build": report.get("build"),
                "detail_status": det.get("status"),
                "detail_retrieved": dh.get("retrieved_total"),
                "detail_ok": ds.get("DETAIL_OK") or ds.get("detail_recovered"),
                "docs": ds.get("documents_downloaded") or ds.get("documents"),
                "fail": dh.get("detail_failure_counts"),
                "og_status": og.get("status"),
                "og_raw": ((og.get("result") or {}).get("raw_opportunities")),
                "og_entities": ((og.get("result") or {}).get("entities_successful")),
                "bn_status": {
                    k: (report.get("bidnet_status") or {}).get(k)
                    for k in (
                        "harvested",
                        "pagination_pct",
                        "pagination_complete",
                        "discovery_truncated",
                        "details_recovered",
                        "documents_recovered",
                    )
                },
                "og_portal": {
                    k: (report.get("opengov_status") or {}).get(k)
                    for k in (
                        "status",
                        "entities_successful",
                        "raw_opportunities",
                        "canonical_live",
                    )
                },
            },
            default=str,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
