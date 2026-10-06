"""Start / status exact-page price extraction job (detached async).

  python scripts/run_exact_page_extract_v1.py --start --workers 4
  python scripts/run_exact_page_extract_v1.py --status
  python scripts/run_exact_page_extract_v1.py --foreground --workers 4
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


def _spawn(*, workers: int, fresh: bool) -> dict:
    from exact_page_extraction.models import BUILD, CK, JOB, REPORT
    from m3_data_root import data_path

    log_path = data_path("m3_exact_page_extract_v1_worker.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--foreground", "--workers", str(workers)]
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
        "job_id": f"EPE-DETACHED-{proc.pid}",
        "build": BUILD,
        "status": "RUNNING",
        "pid": proc.pid,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "progress": {"phase": "SPAWNED", "pct": 0},
        "workers": workers,
        "result_path": REPORT,
        "checkpoint_path": CK,
        "log_path": str(log_path),
        "mode": "detached_subprocess",
    }
    data_path(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
    return {"ok": True, "job_id": job["job_id"], "pid": proc.pid, "log_path": str(log_path), "job": job}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--foreground", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    from exact_page_extraction.models import BUILD, CK, JOB, REPORT, TERMINAL
    from exact_page_extraction.sweep import format_completion_report, run_exact_page_extract_v1
    from m3_data_root import data_path

    if args.status:
        job = json.loads(data_path(JOB).read_text(encoding="utf-8")) if data_path(JOB).exists() else {"status": "NO_JOB"}
        ck = json.loads(data_path(CK).read_text(encoding="utf-8")) if data_path(CK).exists() else {}
        items = ck.get("items") or {}
        print(
            json.dumps(
                {
                    "job": job,
                    "checkpoint": {
                        "n_items": len(items),
                        "terminal": sum(1 for v in items.values() if v.get("status") in TERMINAL),
                        "priced": sum(1 for v in items.values() if (v.get("found") or {}).get("usable")),
                        "report_ready": ck.get("report_ready"),
                        "full100": ck.get("full100"),
                        "easy25": ck.get("easy25"),
                    },
                },
                indent=2,
                default=str,
            )
        )
        if data_path(REPORT).exists() and ck.get("report_ready"):
            print("\n" + format_completion_report(json.loads(data_path(REPORT).read_text(encoding="utf-8"))))
        return 0

    if args.start:
        if args.fresh:
            for name in (CK, REPORT):
                p = data_path(name)
                if p.exists():
                    p.unlink()
        print(json.dumps(_spawn(workers=args.workers, fresh=args.fresh), indent=2))
        return 0

    if args.foreground:
        if args.fresh:
            for name in (CK, REPORT):
                p = data_path(name)
                if p.exists():
                    p.unlink()
        job_path = data_path(JOB)
        job_meta = {
            "job_id": f"EPE-FG-{os.getpid()}",
            "build": BUILD,
            "status": "RUNNING",
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "progress": {"phase": "STARTING", "pct": 0},
            "workers": args.workers,
            "mode": "foreground",
        }
        job_path.write_text(json.dumps(job_meta, indent=2), encoding="utf-8")

        def progress(**kw):
            print(
                f"  {kw.get('pct', 0):3}% {kw.get('phase')} done={kw.get('completed')} rem={kw.get('remaining')}",
                flush=True,
            )
            job_meta["progress"] = dict(kw)
            job_meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            job_path.write_text(json.dumps(job_meta, indent=2), encoding="utf-8")

        try:
            # Raise browser budget for exact-page focused run
            from price_adapters import browser as br

            br._BROWSER_BUDGET["max_renders"] = 120
            report = run_exact_page_extract_v1(resume=not args.fresh, workers=args.workers, on_progress=progress)
            job_meta["status"] = "COMPLETED"
            job_meta["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            job_meta["summary"] = {
                "full100_pass": (report.get("full100") or {}).get("pass"),
                "coverage": (report.get("full100") or {}).get("coverage"),
                "accuracy": (report.get("full100") or {}).get("accuracy"),
                "new_recoveries": (report.get("miss_corpus_result") or {}).get("new_valid_recoveries"),
                "safe_to_scale": report.get("scale_safe"),
            }
            job_path.write_text(json.dumps(job_meta, indent=2), encoding="utf-8")
            print(format_completion_report(report))
            return 0
        except Exception as exc:
            job_meta["status"] = "FAILED"
            job_meta["error"] = str(exc)
            job_path.write_text(json.dumps(job_meta, indent=2), encoding="utf-8")
            raise

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
