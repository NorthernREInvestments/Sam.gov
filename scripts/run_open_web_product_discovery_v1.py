"""Run open-web product discovery job.

  python scripts/run_open_web_product_discovery_v1.py --start --fresh
  python scripts/run_open_web_product_discovery_v1.py --foreground --fresh
  python scripts/run_open_web_product_discovery_v1.py --status
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
    from open_web_product_discovery.models import BUILD, CK, JOB, REPORT

    log_path = data_path("m3_open_web_product_discovery_v1_worker.log")
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
        "job_id": f"OWP-DETACHED-{proc.pid}",
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
    from open_web_product_discovery.models import BUILD, CK, JOB, REPORT
    from open_web_product_discovery.sweep import build_final_report, format_report, run_open_web_product_discovery_v1

    if args.status:
        job = json.loads(data_path(JOB).read_text(encoding="utf-8")) if data_path(JOB).exists() else {"status": "NO_JOB"}
        ck = json.loads(data_path(CK).read_text(encoding="utf-8")) if data_path(CK).exists() else {}
        items = ck.get("items") or {}
        print(
            json.dumps(
                {
                    "job": job,
                    "checkpoint": {
                        "build": ck.get("build"),
                        "n_items": len(items),
                        "ready": sum(1 for v in items.values() if int(v.get("n_validated") or 0) > 0),
                        "report_ready": ck.get("report_ready"),
                    },
                },
                indent=2,
                default=str,
            )
        )
        if data_path(REPORT).exists() and ck.get("report_ready"):
            print("\n" + format_report(json.loads(data_path(REPORT).read_text(encoding="utf-8"))))
        return 0

    if args.report:
        report = build_final_report()
        print(format_report(report))
        print(json.dumps(report.get("FINAL_ANSWERS"), indent=2, default=str))
        return 0

    if args.start and not args.foreground:
        print(json.dumps(_spawn(fresh=args.fresh), indent=2))
        return 0

    if args.foreground or args.start:
        print(f"govtracker: open-web product discovery ({BUILD})", flush=True)
        job = {
            "job_id": f"OWP-FG-{os.getpid()}",
            "build": BUILD,
            "status": "RUNNING",
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        data_path(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
        try:
            report = run_open_web_product_discovery_v1(fresh=args.fresh)
            job["status"] = "COMPLETE"
            job["ready"] = (report.get("READY_FOR_PRICE_EXTRACTION") or {}).get("count")
            job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            data_path(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "ok": True,
                        "FINAL_ANSWERS": report.get("FINAL_ANSWERS"),
                        "EXACT_URL_DISCOVERY": report.get("EXACT_URL_DISCOVERY"),
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

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
