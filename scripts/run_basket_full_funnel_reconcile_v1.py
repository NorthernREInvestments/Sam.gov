"""Run basket completion + canonical full-funnel reconciliation.

  python scripts/run_basket_full_funnel_reconcile_v1.py --start --fresh
  python scripts/run_basket_full_funnel_reconcile_v1.py --status
  python scripts/run_basket_full_funnel_reconcile_v1.py --report
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _spawn(*, fresh: bool) -> dict:
    from m3_data_root import data_path
    from basket_full_funnel_reconcile.models import BUILD, CK, JOB, REPORT

    log_path = data_path("m3_basket_full_funnel_reconcile_v1_worker.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--foreground"]
    if fresh:
        cmd.append("--fresh")
    creationflags = 0x00000008 | 0x00000200 | 0x08000000 if sys.platform == "win32" else 0
    with open(log_path, "a", encoding="utf-8") as logf:
        logf.write(f"\n=== spawn {time.strftime('%Y-%m-%d %H:%M:%S')} {BUILD} ===\n")
        logf.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    job = {
        "job_id": f"BFF-DETACHED-{proc.pid}",
        "build": BUILD,
        "status": "RUNNING",
        "pid": proc.pid,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "result_path": REPORT,
        "checkpoint_path": CK,
        "log_path": str(log_path),
    }
    data_path(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
    return {"ok": True, "job_id": job["job_id"], "pid": proc.pid, "log_path": str(log_path)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--foreground", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--no-capture", action="store_true", help="Skip live BOTH_SIDES ID recapture")
    args = ap.parse_args()

    from m3_data_root import data_path
    from basket_full_funnel_reconcile.models import BUILD, CK, JOB, REPORT
    from basket_full_funnel_reconcile.sweep import (
        build_final_report,
        format_report,
        run_basket_full_funnel_reconcile_v1,
    )

    if args.status:
        job = json.loads(data_path(JOB).read_text(encoding="utf-8")) if data_path(JOB).exists() else {"status": "NO_JOB"}
        ck = json.loads(data_path(CK).read_text(encoding="utf-8")) if data_path(CK).exists() else {}
        opps = ck.get("opportunities") or {}
        print(
            json.dumps(
                {
                    "job": job,
                    "build": BUILD,
                    "processed": len(opps),
                    "terminals": sum(1 for r in opps.values() if (r.get("economics") or {}).get("economic_terminal")),
                    "report_ready": ck.get("report_ready"),
                },
                indent=2,
                default=str,
            )
        )
        return 0

    if args.report:
        report = build_final_report()
        print(format_report(report))
        print(json.dumps({"FINAL_ANSWERS": report.get("FINAL_ANSWERS"), "SCALE_DECISION": report.get("SCALE_DECISION")}, indent=2, default=str))
        return 0

    if args.start and not args.foreground:
        print(json.dumps(_spawn(fresh=args.fresh), indent=2))
        return 0

    job = {
        "job_id": f"BFF-FG-{int(time.time())}",
        "build": BUILD,
        "status": "RUNNING",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "result_path": REPORT,
        "checkpoint_path": CK,
    }
    data_path(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
    try:
        report = run_basket_full_funnel_reconcile_v1(
            fresh=args.fresh,
            capture_both_sides=not args.no_capture,
        )
        job["status"] = "DONE"
        job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        data_path(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "ok": True,
                    "FINAL_ANSWERS": report.get("FINAL_ANSWERS"),
                    "BASKET_READY": report.get("BASKET_READY"),
                    "ECONOMICS": report.get("ECONOMICS"),
                    "PROFIT": report.get("PROFIT"),
                    "SCALE_DECISION": report.get("SCALE_DECISION"),
                },
                indent=2,
                default=str,
            )
        )
        return 0
    except Exception as exc:
        job["status"] = "FAILED"
        job["error"] = str(exc)
        data_path(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
