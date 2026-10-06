"""Source Repair V2 validation — staged async jobs, bounded polls, economics handoff.

Respects execution-time rules: no 20–30 min shell waits; poll windows ~5 min;
long harvests continue server-side; re-poll / checkpoint as needed.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
OUT = ROOT / "data" / "source_repair_v2_report.json"
POLL_SEC = 15
MAX_POLLS = 20  # ~5 min per window


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
            f"pct={prog.get('pct')} retrieved={prog.get('retrieved')} "
            f"partition={prog.get('partition')} entities={prog.get('entities')}",
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
    health = c.get("/api/health", timeout=30).json()
    report: dict = {"build": health.get("build_version"), "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # Capture before canonical
    try:
        cov0 = c.get("/api/m3/source-coverage/production", timeout=60).json()
        report["before"] = {
            "canonical_live": (cov0.get("combined") or {}).get("canonical_live"),
            "canonical_total": (cov0.get("combined") or {}).get("canonical_total"),
        }
    except Exception as exc:
        report["before"] = {"error": type(exc).__name__}

    # E1 BidNet partitioned full
    r = c.post(
        "/api/m3/bidnet-discovery/harvest",
        json={
            "max_results": 30000,
            "max_pages": 400,
            "open_details": False,
            "persist": True,
            "async": True,
            "mode": "partitioned",
        },
    )
    print("start bidnet_partitioned", r.status_code, r.text[:400], flush=True)
    jid = r.json().get("job_id")
    report["bidnet_job_id"] = jid
    report["bidnet"] = poll(c, jid, "bidnet_partitioned", max_polls=20) if jid else r.json()

    # If still running after first window, note checkpoint and continue later windows
    if (report.get("bidnet") or {}).get("status") == "RUNNING" or (report.get("bidnet") or {}).get("poll_timeout"):
        print("bidnet still running — second poll window", flush=True)
        report["bidnet"] = poll(c, jid, "bidnet_partitioned_w2", max_polls=20)

    # E2 OpenGov all known (~142) via public-first
    r = c.post(
        "/api/m3/opengov-discovery/run",
        json={"max_entities": 200, "max_pages": 6, "persist": True, "async": True},
    )
    print("start opengov_all", r.status_code, r.text[:400], flush=True)
    jid = r.json().get("job_id")
    report["opengov_job_id"] = jid
    report["opengov"] = poll(c, jid, "opengov_all", max_polls=20) if jid else r.json()
    if (report.get("opengov") or {}).get("poll_timeout"):
        report["opengov"] = poll(c, jid, "opengov_all_w2", max_polls=16)

    # E3 Euna central
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
    print("start euna_central", r.status_code, r.text[:400], flush=True)
    jid = r.json().get("job_id")
    report["euna_job_id"] = jid
    report["euna"] = poll(c, jid, "euna_central", max_polls=20) if jid else r.json()

    # Re-check BidNet if still not done
    bn_id = report.get("bidnet_job_id")
    if bn_id:
        bn = c.get(f"/api/m3/auth-jobs/{bn_id}", timeout=30).json()
        report["bidnet_final"] = bn
        if bn.get("status") == "RUNNING":
            print("bidnet still RUNNING — third poll window", flush=True)
            report["bidnet_final"] = poll(c, bn_id, "bidnet_partitioned_w3", max_polls=20)

    # Coverage + status
    report["coverage"] = c.get("/api/m3/source-coverage/production", timeout=60).json()
    report["bidnet_status"] = c.get("/api/m3/bidnet-auth/status").json()
    report["opengov_status"] = c.get("/api/m3/opengov-auth/status").json()
    report["euna_status"] = c.get("/api/m3/euna-auth/status").json()

    # Economics / universe pass (existing pipeline — do not rewrite)
    print("start universe_pass", flush=True)
    try:
        up = c.post(
            "/api/ui/universe-pass/run",
            json={"force_reclassify": False},
            timeout=180,
        )
        report["universe_pass"] = up.json() if up.status_code < 500 else {"http": up.status_code, "text": up.text[:500]}
    except Exception as exc:
        report["universe_pass"] = {"error": type(exc).__name__}

    report["after"] = {
        "canonical_live": (report.get("coverage") or {}).get("combined", {}).get("canonical_live"),
        "canonical_total": (report.get("coverage") or {}).get("combined", {}).get("canonical_total"),
        "owner": (report.get("coverage") or {}).get("owner"),
    }
    report["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT, flush=True)

    owner = (report.get("coverage") or {}).get("owner") or {}
    bn = owner.get("bidnet") or {}
    og = owner.get("opengov") or {}
    eu = owner.get("euna") or {}
    up = report.get("universe_pass") or {}
    print(
        "PART_I_SUMMARY",
        json.dumps(
            {
                "build": report.get("build"),
                "bn_retrieved": bn.get("retrieved"),
                "bn_pct": bn.get("retrieval_pct"),
                "bn_complete": bn.get("pagination_complete"),
                "bn_net_new": bn.get("net_new"),
                "og_public": og.get("public_working"),
                "og_raw": og.get("raw"),
                "og_net_new": og.get("net_new"),
                "eu_reachable": eu.get("central_reachable"),
                "eu_retrieved": eu.get("retrieved"),
                "eu_net_new": eu.get("net_new"),
                "before_live": (report.get("before") or {}).get("canonical_live"),
                "after_live": (report.get("after") or {}).get("canonical_live"),
                "product": (up.get("classification") or {}).get("TANGIBLE_PRODUCT"),
                "economics": (up.get("product_economics") or {}).get("economics_ready"),
                "profitable": (up.get("product_economics") or {}).get("profitable_at_public_retail"),
            },
            default=str,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
