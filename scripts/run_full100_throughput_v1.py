"""Start / status Full-100 throughput job (async).

  # start (returns immediately; spawns detached worker)
  python scripts/run_full100_throughput_v1.py --start

  # poll status
  python scripts/run_full100_throughput_v1.py --status

  # foreground (dev / worker process)
  python scripts/run_full100_throughput_v1.py --foreground --workers 4
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


def _spawn_detached(*, workers: int, fresh: bool) -> dict:
    """Spawn a durable background worker that outlives this CLI process."""
    from full100_throughput.models import BUILD, CK, JOB, REPORT
    from m3_data_root import data_path

    log_path = data_path("m3_full100_throughput_v1_worker.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--foreground",
        "--workers",
        str(workers),
    ]
    if fresh:
        cmd.append("--fresh")

    creationflags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        creationflags = 0x00000008 | 0x00000200 | 0x08000000

    with open(log_path, "a", encoding="utf-8") as logf:
        logf.write(f"\n=== spawn {time.strftime('%Y-%m-%d %H:%M:%S')} build={BUILD} ===\n")
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
        "job_id": f"F100-DETACHED-{proc.pid}",
        "build": BUILD,
        "status": "RUNNING",
        "pid": proc.pid,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "progress": {"phase": "SPAWNED", "pct": 0, "completed": 0, "remaining": 0, "active": workers},
        "resume": not fresh,
        "workers": workers,
        "error": None,
        "result_path": REPORT,
        "checkpoint_path": CK,
        "log_path": str(log_path),
        "mode": "detached_subprocess",
    }
    jp = data_path(JOB)
    jp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    return {"ok": True, "job_id": job["job_id"], "pid": proc.pid, "log_path": str(log_path), "job": job}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", action="store_true", help="Start detached Full-100 worker")
    ap.add_argument("--status", action="store_true", help="Print latest job/checkpoint status")
    ap.add_argument("--foreground", action="store_true", help="Run in-process (worker)")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--wait", type=float, default=0.0, help="After --start, poll up to N seconds")
    args = ap.parse_args()

    from full100_throughput.job import get_job
    from full100_throughput.models import CK, REPORT
    from full100_throughput.sweep import format_completion_report, run_full100_throughput_v1
    from m3_data_root import data_path

    if args.fresh and args.foreground:
        for name in (CK, REPORT):
            p = data_path(name)
            if p.exists():
                p.unlink()

    if args.status:
        job = get_job()
        ck = {}
        cp = data_path(CK)
        if cp.exists():
            try:
                ck = json.loads(cp.read_text(encoding="utf-8"))
            except Exception:
                ck = {}
        items = ck.get("items") or {}
        from full100_throughput.models import TERMINAL

        done = sum(1 for v in items.values() if v.get("status") in TERMINAL)
        out = {
            "job": job or {"status": "NO_JOB"},
            "checkpoint": {
                "n_items": len(items),
                "terminal": done,
                "report_ready": ck.get("report_ready"),
                "full100": ck.get("full100"),
                "easy25": ck.get("easy25"),
            },
        }
        print(json.dumps(out, indent=2, default=str))
        rep = data_path(REPORT)
        if rep.exists() and (ck.get("report_ready") or (job or {}).get("status") == "COMPLETED"):
            report = json.loads(rep.read_text(encoding="utf-8"))
            print("\n" + format_completion_report(report))
        return 0

    if args.start:
        if args.fresh:
            for name in (CK, REPORT):
                p = data_path(name)
                if p.exists():
                    p.unlink()
        out = _spawn_detached(workers=args.workers, fresh=args.fresh)
        print(json.dumps(out, indent=2, default=str))
        if args.wait and args.wait > 0:
            deadline = time.time() + args.wait
            while time.time() < deadline:
                time.sleep(min(15.0, max(3.0, args.wait / 30)))
                cp = data_path(CK)
                ck = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {}
                items = ck.get("items") or {}
                from full100_throughput.models import TERMINAL

                done = sum(1 for v in items.values() if v.get("status") in TERMINAL)
                print(f"  terminal={done}/{len(items)} report_ready={ck.get('report_ready')}", flush=True)
                if ck.get("report_ready"):
                    break
            if data_path(REPORT).exists():
                print("\n" + format_completion_report(json.loads(data_path(REPORT).read_text(encoding="utf-8"))))
        return 0

    if args.foreground:
        def progress(**kw):
            print(
                f"  {kw.get('pct', 0):3}% {kw.get('phase')} "
                f"done={kw.get('completed')} rem={kw.get('remaining')} active={kw.get('active')} eta={kw.get('eta_s')}",
                flush=True,
            )

        # Also update JOB file for status polling
        from full100_throughput.models import BUILD, JOB

        job_path = data_path(JOB)
        job_meta = {
            "job_id": f"F100-FG-{os.getpid()}",
            "build": BUILD,
            "status": "RUNNING",
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "progress": {"phase": "STARTING", "pct": 0},
            "workers": args.workers,
            "mode": "foreground",
        }
        job_path.write_text(json.dumps(job_meta, indent=2), encoding="utf-8")

        def progress_persist(**kw):
            progress(**kw)
            job_meta["progress"] = {
                "phase": kw.get("phase"),
                "pct": kw.get("pct"),
                "completed": kw.get("completed"),
                "remaining": kw.get("remaining"),
                "active": kw.get("active"),
                "eta_s": kw.get("eta_s"),
            }
            job_meta["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            job_path.write_text(json.dumps(job_meta, indent=2), encoding="utf-8")

        try:
            report = run_full100_throughput_v1(
                resume=not args.fresh,
                workers=args.workers,
                on_progress=progress_persist,
            )
            job_meta["status"] = "COMPLETED"
            job_meta["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            job_meta["summary"] = {
                "full100_pass": (report.get("full100") or {}).get("pass"),
                "coverage": (report.get("full100") or {}).get("coverage"),
                "accuracy": (report.get("full100") or {}).get("accuracy"),
                "safe_to_scale": report.get("scale_safe"),
            }
            job_path.write_text(json.dumps(job_meta, indent=2), encoding="utf-8")
            print(format_completion_report(report))
            print(f"\nReport: {data_path(REPORT)}")
            return 0
        except Exception as exc:
            job_meta["status"] = "FAILED"
            job_meta["error"] = str(exc)
            job_meta["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            job_path.write_text(json.dumps(job_meta, indent=2), encoding="utf-8")
            raise

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
