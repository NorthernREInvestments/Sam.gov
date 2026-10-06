"""Fetch E2E last report summary from production."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "FULL_PRODUCTION_E2E_REPORT.json"
BASE = "https://samgov-production.up.railway.app"


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
    c = httpx.Client(base_url=BASE, timeout=120, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    job = c.get("/api/m3/auth-jobs/E2E-b8b6c8203aa8", timeout=60).json()
    disk = c.get("/api/m3/full-production-e2e/last-report", timeout=90).json()
    payload = {
        "job_status": job.get("status"),
        "progress": job.get("progress"),
        "result_keys": list((job.get("result") or {}).keys()) if isinstance(job.get("result"), dict) else None,
        "result_summary": {
            k: (job.get("result") or {}).get(k)
            for k in (
                "canonical_before",
                "canonical_after",
                "product_funnel",
                "economics",
                "owner_candidates",
                "drop_off",
            )
            if isinstance(job.get("result"), dict)
        },
        "disk_report": disk if isinstance(disk, dict) else {"raw": str(disk)[:500]},
    }
    # Trim huge nested
    if isinstance(payload["disk_report"], dict):
        dr = dict(payload["disk_report"])
        for k in list(dr.keys()):
            if k in {"phases", "samples", "owner_candidates"}:
                continue
        payload["disk_keys"] = list(dr.keys())
        for key in (
            "kind",
            "run_id",
            "canonical_before",
            "canonical_after",
            "product_classification",
            "economics_summary",
            "profit_buckets",
            "end_of_funnel",
            "source_attribution",
            "drop_off",
            "questions",
        ):
            if key in dr:
                payload[key] = dr[key]
        # compact phases
        phases = dr.get("phases") if isinstance(dr.get("phases"), dict) else {}
        payload["phases_compact"] = {
            k: {
                kk: vv
                for kk, vv in (v.items() if isinstance(v, dict) else [])
                if kk
                not in {
                    "per_entity",
                    "samples",
                    "portal_status_counts",
                    "resolver_telemetry",
                }
            }
            if isinstance(v, dict)
            else v
            for k, v in phases.items()
        }
    OUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: payload.get(k) for k in ("job_status", "canonical_before", "canonical_after", "disk_keys", "phases_compact")}, indent=2, default=str)[:6000])
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
