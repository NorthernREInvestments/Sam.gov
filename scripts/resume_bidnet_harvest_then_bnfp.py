"""Resume polling existing BidNet partitioned harvest, then rebuild BNFP report."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
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
    jid_path = ROOT / "data" / "m3_bidnet_partitioned_harvest_job_id.txt"
    jid = jid_path.read_text(encoding="utf-8").strip() if jid_path.exists() else "BNH-871f45bb59c6"
    c = httpx.Client(base_url=BASE, timeout=120, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    print("resume_poll", jid, flush=True)

    harvest = None
    for i in range(480):
        d = c.get(f"/api/m3/auth-jobs/{jid}").json()
        prog = d.get("progress") or {}
        res = d.get("result") or {}
        print(
            f"poll {i}: status={d.get('status')} phase={prog.get('phase')} pct={prog.get('pct')} "
            f"retrieved={prog.get('retrieved') or res.get('retrieved_unique')} "
            f"partition={prog.get('partition')} err={(d.get('error') or '')[:120]}",
            flush=True,
        )
        if d.get("status") in {"COMPLETED", "FAILED"}:
            harvest = d
            break
        time.sleep(30)
    else:
        print("harvest_timeout", flush=True)
        return 1

    (ROOT / "data" / "m3_bidnet_partitioned_harvest_prod.json").write_text(
        json.dumps(harvest, indent=2, default=str), encoding="utf-8"
    )
    res = harvest.get("result") or {}
    print(
        f"HARVEST_DONE status={harvest.get('status')} unique={res.get('retrieved_unique')} "
        f"reported={res.get('reported_open_ui')} pct={res.get('retrieval_pct')} "
        f"complete={res.get('pagination_complete')} truncated={res.get('DISCOVERY_TRUNCATED')}",
        flush=True,
    )
    if harvest.get("status") != "COMPLETED":
        return 1

    rr = c.post(
        "/api/m3/bidnet-full-production/run",
        json={"fresh": False, "full_discovery": False, "async": True},
    )
    print("bnfp_kick", rr.status_code, rr.text[:500], flush=True)
    rr.raise_for_status()
    bnfp_id = rr.json()["job_id"]
    for j in range(180):
        b = c.get(f"/api/m3/auth-jobs/{bnfp_id}").json()
        bp = b.get("progress") or {}
        br = b.get("result") or {}
        print(
            f"bnfp {j}: status={b.get('status')} phase={bp.get('phase')} pct={bp.get('pct')} "
            f"PASS={br.get('BIDNET_PRODUCTION_PASS')} err={(b.get('error') or '')[:120]}",
            flush=True,
        )
        if b.get("status") in {"COMPLETED", "FAILED"}:
            (ROOT / "data" / "m3_bidnet_full_production_v1_prod_report.json").write_text(
                json.dumps({"job": b, "report": br}, indent=2, default=str),
                encoding="utf-8",
            )
            print(
                f"FINAL status={b.get('status')} BIDNET_PRODUCTION_PASS={br.get('BIDNET_PRODUCTION_PASS')} "
                f"acc={(br.get('acceptance_192') or {}).get('PASS_FAIL')} "
                f"source_health={br.get('source_health')} discovery={br.get('discovery')}",
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
            ok = b.get("status") == "COMPLETED" and br.get("BIDNET_PRODUCTION_PASS") == "YES"
            return 0 if ok else 2
        time.sleep(15)
    print("bnfp_timeout", flush=True)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
