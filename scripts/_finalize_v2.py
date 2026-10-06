"""Kick Euna central + universe economics pass after BidNet/OpenGov."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "source_repair_v2_final.json"


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


def poll(c: httpx.Client, job_id: str, max_polls: int = 20) -> dict:
    last: dict = {}
    for i in range(max_polls):
        try:
            d = c.get(f"/api/m3/auth-jobs/{job_id}", timeout=30).json()
        except Exception as exc:
            print(f"poll {i}: err={type(exc).__name__}", flush=True)
            time.sleep(15)
            continue
        p = d.get("progress") or {}
        print(
            f"poll {i}: status={d.get('status')} phase={p.get('phase')} "
            f"pct={p.get('pct')} retrieved={p.get('retrieved')}",
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
    c = httpx.Client(base_url=BASE, timeout=90, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    report: dict = {
        "build": c.get("/api/health").json().get("build_version"),
        "before_coverage": c.get("/api/m3/source-coverage/production").json(),
    }

    r = c.post(
        "/api/m3/euna-discovery/run",
        json={
            "mode": "central",
            "max_results": 10000,
            "max_pages": 50,
            "persist": True,
            "async": True,
        },
    )
    print("euna_start", r.status_code, r.text[:300], flush=True)
    jid = r.json().get("job_id")
    report["euna"] = poll(c, jid, max_polls=24) if jid else r.json()

    print("universe_pass", flush=True)
    try:
        up = c.post(
            "/api/ui/universe-pass/run",
            json={"force_reclassify": False},
            timeout=180,
        )
        report["universe_pass"] = (
            up.json() if up.status_code < 500 else {"http": up.status_code, "text": up.text[:800]}
        )
    except Exception as exc:
        report["universe_pass"] = {"error": type(exc).__name__}

    report["coverage"] = c.get("/api/m3/source-coverage/production").json()
    report["bidnet_job"] = c.get("/api/m3/auth-jobs/BNH-42de029770ad").json()
    report["opengov_job"] = c.get("/api/m3/auth-jobs/OGD-8415e665d00e").json()
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT, flush=True)
    owner = (report.get("coverage") or {}).get("owner") or {}
    up = report.get("universe_pass") or {}
    print(
        "FINAL",
        json.dumps(
            {
                "bn": owner.get("bidnet"),
                "og": owner.get("opengov"),
                "eu": owner.get("euna"),
                "combined": (report.get("coverage") or {}).get("combined"),
                "product": (up.get("classification") or {}).get("TANGIBLE_PRODUCT"),
                "economics": (up.get("product_economics") or {}),
            },
            default=str,
        )[:2000],
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
