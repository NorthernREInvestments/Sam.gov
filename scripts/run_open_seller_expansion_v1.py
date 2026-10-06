"""Run open alternate seller expansion.

  python scripts/run_open_seller_expansion_v1.py --foreground --fresh
  python scripts/run_open_seller_expansion_v1.py --status
  python scripts/run_open_seller_expansion_v1.py --report
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
    from open_seller_expansion.models import BUILD, CK, JOB, REPORT

    log_path = data_path("m3_open_seller_expansion_v1_worker.log")
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
        "job_id": f"OSE-DETACHED-{proc.pid}",
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
    args = ap.parse_args()

    from m3_data_root import data_path
    from open_seller_expansion.models import BUILD, CK, JOB, REPORT
    from open_seller_expansion.sweep import build_final_report, format_report, run_open_seller_expansion_v1

    if args.status:
        job = json.loads(data_path(JOB).read_text(encoding="utf-8")) if data_path(JOB).exists() else {"status": "NO_JOB"}
        ck = json.loads(data_path(CK).read_text(encoding="utf-8")) if data_path(CK).exists() else {}
        items = ck.get("items") or {}
        got = sum(1 for r in items.values() if r.get("status") == "EXECUTABLE_PRICE")
        print(
            json.dumps(
                {
                    "job": job,
                    "build": BUILD,
                    "processed": len(items),
                    "recovered": got,
                    "projected_total": 46 + got,
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
        print(json.dumps({"FINAL_ANSWERS": report.get("FINAL_ANSWERS"), "FULL100": report.get("FULL100")}, indent=2, default=str))
        return 0

    if args.start and not args.foreground:
        out = _spawn(fresh=args.fresh)
        print(json.dumps(out, indent=2))
        return 0

    # foreground
    from m3_data_root import data_path as dp

    job = {
        "job_id": f"OSE-FG-{int(time.time())}",
        "build": BUILD,
        "status": "RUNNING",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "result_path": REPORT,
        "checkpoint_path": CK,
    }
    dp(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
    try:
        report = run_open_seller_expansion_v1(fresh=args.fresh)
        job["status"] = "DONE"
        job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        job["final_priced"] = (report.get("FULL100") or {}).get("final_valid")
        dp(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {"ok": True, "FINAL_ANSWERS": report.get("FINAL_ANSWERS"), "FULL100": report.get("FULL100")},
                indent=2,
                default=str,
            )
        )
        return 0
    except Exception as exc:
        job["status"] = "FAILED"
        job["error"] = str(exc)
        dp(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
