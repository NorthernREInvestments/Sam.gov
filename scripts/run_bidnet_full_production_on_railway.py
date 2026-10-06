"""Deploy wait + kick BidNet full production on Railway; poll until done."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://samgov-production.up.railway.app"
TARGET = "20261005-m3-bidnet-full-production-v1"


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


def client() -> httpx.Client:
    c = httpx.Client(base_url=BASE, timeout=120, follow_redirects=True)
    c.post(
        "/api/login",
        json={"email": os.environ["APP_EMAIL"], "password": os.environ["APP_PASSWORD"]},
    ).raise_for_status()
    return c


def wait_deploy(max_polls: int = 90, poll_sec: int = 15) -> bool:
    started = time.time()
    ok = 0
    for i in range(max_polls):
        try:
            h = httpx.get(f"{BASE}/api/health", timeout=30).json()
            build = h.get("build_version")
            print(f"deploy_poll {i}: build={build} elapsed={int(time.time()-started)}s", flush=True)
            if build == TARGET:
                r = httpx.get(f"{BASE}/api/m3/bidnet-full-production/report", timeout=30)
                print(f"  endpoint_report={r.status_code}", flush=True)
                if r.status_code < 500:
                    ok += 1
                    if ok >= 2:
                        return True
            else:
                ok = 0
        except Exception as exc:
            ok = 0
            print(f"deploy_poll {i}: err={type(exc).__name__}", flush=True)
        time.sleep(poll_sec)
    return False


def main() -> int:
    _load_dotenv()
    mode = "kick"
    if "--wait-deploy" in sys.argv:
        mode = "wait"
    if "--poll" in sys.argv:
        mode = "poll"
    if "--seed" in sys.argv:
        mode = "seed"
    if "--env-check" in sys.argv:
        mode = "env"
    job_id = None
    for a in sys.argv[1:]:
        if a.startswith("--job="):
            job_id = a.split("=", 1)[1]

    if mode == "wait":
        ok = wait_deploy()
        print("deploy_ready" if ok else "deploy_timeout", TARGET, flush=True)
        return 0 if ok else 1

    c = client()
    h = c.get("/api/health").json()
    print("prod_build", h.get("build_version"), flush=True)

    if mode == "env":
        r = c.get("/api/m3/bidnet-full-production/env-check")
        print(json.dumps(r.json(), indent=2), flush=True)
        return 0 if r.json().get("ok") else 1

    if mode == "seed":
        corpus = json.loads((ROOT / "data" / "LARGE_TEST_CORPUS_V1.json").read_text(encoding="utf-8"))
        print("seeding corpus count", corpus.get("count"), flush=True)
        r = c.post("/api/m3/bidnet-full-production/seed-corpus", json=corpus, timeout=180)
        print("seed", r.status_code, r.text[:400], flush=True)
        r.raise_for_status()
        return 0 if r.json().get("ok") else 1

    auth = c.get("/api/m3/bidnet-auth/status").json()
    print(
        "auth",
        {
            "status": auth.get("status"),
            "storage_state_present": auth.get("storage_state_present"),
            "credentials_configured": auth.get("credentials_configured"),
            "reported_open": auth.get("reported_open"),
        },
        flush=True,
    )

    if mode == "kick":
        if h.get("build_version") != TARGET:
            print("REFUSE_KICK: production build is not", TARGET, flush=True)
            return 2
        # env gate
        env = c.get("/api/m3/bidnet-full-production/env-check").json()
        print("env_check", env, flush=True)
        if not env.get("credentials_configured") and not env.get("storage_state_present"):
            print("STOP: no BidNet credentials and no storage_state", flush=True)
            return 3
        if int(env.get("corpus_bidnet") or 0) != 192:
            print("STOP: corpus BidNet != 192 — seed first", flush=True)
            return 4
        full = "--full-discovery" in sys.argv
        # Default: fresh acceptance. With --full-discovery alone, resume 192 checkpoint and only harvest.
        fresh = True
        if full and "--fresh" not in sys.argv:
            fresh = False
        if "--fresh" in sys.argv:
            fresh = True
        r = c.post(
            "/api/m3/bidnet-full-production/run",
            json={"fresh": fresh, "full_discovery": full},
        )
        print("kick", r.status_code, r.text[:500], flush=True)
        r.raise_for_status()
        job_id = r.json().get("job_id")
        print("job_id", job_id, flush=True)
        mode = "poll"

    if mode == "poll" and job_id:
        for i in range(600):  # up to ~5h at 30s — authenticated 192 detail is slow
            d = c.get(f"/api/m3/bidnet-full-production/job/{job_id}").json()
            prog = d.get("progress") or {}
            print(
                f"poll {i}: status={d.get('status')} phase={prog.get('phase')} pct={prog.get('pct')} "
                f"detail={(prog.get('detail') or '')[:80]} err={(d.get('error') or '')[:200]}",
                flush=True,
            )
            if d.get("status") in {"COMPLETED", "FAILED"}:
                report = c.get("/api/m3/bidnet-full-production/report").json()
                out = ROOT / "data" / "m3_bidnet_full_production_v1_prod_report.json"
                out.write_text(json.dumps({"job": d, "report": report}, indent=2, default=str), encoding="utf-8")
                txt = ROOT / "data" / "m3_bidnet_full_production_v1_prod_report_snippet.txt"
                from bidnet_full_production.report import format_report

                if report.get("status") != "NO_REPORT" and report.get("session") is not None:
                    txt.write_text(format_report(report), encoding="utf-8")
                    print(txt.read_text(encoding="utf-8"), flush=True)
                print(
                    "FINAL",
                    d.get("status"),
                    "PASS",
                    (d.get("result") or {}).get("BIDNET_PRODUCTION_PASS") or report.get("BIDNET_PRODUCTION_PASS"),
                    flush=True,
                )
                return 0 if d.get("status") == "COMPLETED" else 1
            time.sleep(30)
        print("poll_timeout", job_id, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
