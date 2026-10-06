"""Wait for new build, kick partitioned harvest, poll until complete, then rebuild BNFP report."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
TARGET_BUILD = "20261006-m3-bidnet-discovery-complete-v1"


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


def _client() -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=120, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    return c


def wait_build(c: httpx.Client) -> None:
    for i in range(60):
        try:
            ec = c.get("/api/m3/bidnet-full-production/env-check")
            if ec.status_code == 200:
                d = ec.json()
                print(
                    f"env {i}: build={d.get('build_version')} ok={d.get('ok')} corpus={d.get('corpus_bidnet')}",
                    flush=True,
                )
                if d.get("ok") and TARGET_BUILD in str(d.get("build_version") or ""):
                    return
        except Exception as exc:
            print(f"env {i}: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(10)
    raise SystemExit("build_wait_timeout")


def poll_job(c: httpx.Client, job_id: str, *, label: str, max_polls: int = 720) -> dict:
    print(f"polling {label} {job_id}", flush=True)
    for i in range(max_polls):
        d = c.get(f"/api/m3/auth-jobs/{job_id}").json()
        prog = d.get("progress") or {}
        res = d.get("result") or {}
        print(
            f"{label} poll {i}: status={d.get('status')} phase={prog.get('phase')} pct={prog.get('pct')} "
            f"retrieved={prog.get('retrieved') or res.get('retrieved_unique')} "
            f"partition={prog.get('partition')} pages={prog.get('pages')} "
            f"err={(d.get('error') or '')[:140]}",
            flush=True,
        )
        if d.get("status") in {"COMPLETED", "FAILED"}:
            return d
        time.sleep(30)
    raise SystemExit(f"{label}_poll_timeout")


def main() -> int:
    _load_dotenv()
    c = _client()
    wait_build(c)

    r = c.post(
        "/api/m3/bidnet-discovery/harvest",
        json={"mode": "partitioned", "max_results": 30000, "max_pages": 1200, "async": True},
    )
    print("harvest_kick", r.status_code, r.text[:500], flush=True)
    r.raise_for_status()
    harvest_id = r.json()["job_id"]
    (ROOT / "data" / "m3_bidnet_partitioned_harvest_job_id.txt").write_text(harvest_id, encoding="utf-8")

    harvest = poll_job(c, harvest_id, label="harvest", max_polls=720)
    (ROOT / "data" / "m3_bidnet_partitioned_harvest_prod.json").write_text(
        json.dumps(harvest, indent=2, default=str), encoding="utf-8"
    )
    if harvest.get("status") != "COMPLETED":
        print("HARVEST_FAILED", flush=True)
        return 1

    res = harvest.get("result") or {}
    print(
        f"HARVEST_OK unique={res.get('retrieved_unique')} reported={res.get('reported_open_ui')} "
        f"pct={res.get('retrieval_pct')} complete={res.get('pagination_complete')} "
        f"truncated={res.get('DISCOVERY_TRUNCATED')}",
        flush=True,
    )

    # Rebuild BNFP report from checkpoint (fresh=False) — do not redo 192 live details.
    rr = c.post(
        "/api/m3/bidnet-full-production/run",
        json={"fresh": False, "full_discovery": False, "async": True},
    )
    print("bnfp_kick", rr.status_code, rr.text[:500], flush=True)
    rr.raise_for_status()
    bnfp_id = rr.json()["job_id"]
    bnfp = poll_job(c, bnfp_id, label="bnfp", max_polls=120)
    (ROOT / "data" / "m3_bidnet_full_production_v1_prod_report.json").write_text(
        json.dumps({"job": bnfp, "report": (bnfp.get("result") or {})}, indent=2, default=str),
        encoding="utf-8",
    )
    result = bnfp.get("result") or {}
    print(
        f"FINAL status={bnfp.get('status')} BIDNET_PRODUCTION_PASS={result.get('BIDNET_PRODUCTION_PASS')} "
        f"acc={(result.get('acceptance_192') or {}).get('PASS_FAIL')} "
        f"source_health={result.get('source_health')} discovery={result.get('discovery')}",
        flush=True,
    )
    try:
        txt = c.get("/api/m3/bidnet-full-production/report?format=text")
        if txt.status_code == 200:
            (ROOT / "data" / "m3_bidnet_full_production_v1_prod_report_snippet.txt").write_text(
                txt.text[:4000], encoding="utf-8"
            )
    except Exception:
        pass
    return 0 if bnfp.get("status") == "COMPLETED" and result.get("BIDNET_PRODUCTION_PASS") == "YES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
