"""Deploy wait -> rebuild BNFP from existing discovery (no re-harvest)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
TARGET = "20261006-m3-bidnet-discovery-complete-v2"


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

    for i in range(60):
        try:
            ec = c.get("/api/m3/bidnet-full-production/env-check").json()
            print(f"env {i}: {ec.get('build_version')} ok={ec.get('ok')}", flush=True)
            if ec.get("ok") and TARGET in str(ec.get("build_version") or ""):
                break
        except Exception as exc:
            print(f"env {i}: {type(exc).__name__}", flush=True)
        time.sleep(10)
    else:
        print("build_timeout", flush=True)
        return 2

    rr = c.post(
        "/api/m3/bidnet-full-production/run",
        json={"fresh": False, "full_discovery": False, "async": True},
    )
    print("bnfp_kick", rr.status_code, rr.text[:400], flush=True)
    rr.raise_for_status()
    jid = rr.json()["job_id"]
    for j in range(180):
        b = c.get(f"/api/m3/auth-jobs/{jid}").json()
        bp = b.get("progress") or {}
        br = b.get("result") or {}
        print(
            f"bnfp {j}: status={b.get('status')} phase={bp.get('phase')} pct={bp.get('pct')} "
            f"PASS={br.get('BIDNET_PRODUCTION_PASS')} err={(b.get('error') or '')[:120]}",
            flush=True,
        )
        if b.get("status") in {"COMPLETED", "FAILED"}:
            report = br
            try:
                report = c.get("/api/m3/bidnet-full-production/report").json()
            except Exception:
                pass
            (ROOT / "data" / "m3_bidnet_full_production_v1_prod_report.json").write_text(
                json.dumps({"job": b, "report": report}, indent=2, default=str),
                encoding="utf-8",
            )
            print(
                f"FINAL status={b.get('status')} BIDNET_PRODUCTION_PASS={report.get('BIDNET_PRODUCTION_PASS')} "
                f"acc={(report.get('acceptance_192') or {}).get('PASS_FAIL')} "
                f"source_health={report.get('source_health')} discovery={report.get('discovery')}",
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
            ok = b.get("status") == "COMPLETED" and report.get("BIDNET_PRODUCTION_PASS") == "YES"
            return 0 if ok else 1
        time.sleep(10)
    print("bnfp_timeout", flush=True)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
