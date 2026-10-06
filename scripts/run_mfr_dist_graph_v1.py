"""Start / status manufacturer-distributor graph job (detached async).

  python scripts/run_mfr_dist_graph_v1.py --start --workers 4
  python scripts/run_mfr_dist_graph_v1.py --status
  python scripts/run_mfr_dist_graph_v1.py --foreground --workers 4
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
    from manufacturer_distributor_graph.models import BUILD, CK, JOB, REPORT
    from m3_data_root import data_path

    log_path = data_path("m3_mfr_dist_graph_v1_worker.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--foreground", "--workers", str(workers)]
    if fresh:
        cmd.append("--fresh")
    creationflags = 0
    if sys.platform == "win32":
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
        "job_id": f"MDG-DETACHED-{proc.pid}",
        "build": BUILD,
        "status": "RUNNING",
        "pid": proc.pid,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "progress": {"phase": "SPAWNED", "pct": 0},
        "resume": not fresh,
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

    from manufacturer_distributor_graph.models import CK, JOB, REPORT, TERMINAL, BUILD
    from manufacturer_distributor_graph.sweep import format_completion_report, run_mfr_dist_graph_v1
    from m3_data_root import data_path

    if args.status:
        job = {}
        jp = data_path(JOB)
        if jp.exists():
            job = json.loads(jp.read_text(encoding="utf-8"))
        ck = {}
        cp = data_path(CK)
        if cp.exists():
            ck = json.loads(cp.read_text(encoding="utf-8"))
        items = ck.get("items") or {}
        done = sum(1 for v in items.values() if v.get("status") in TERMINAL)
        print(
            json.dumps(
                {
                    "job": job or {"status": "NO_JOB"},
                    "checkpoint": {
                        "n_items": len(items),
                        "terminal": done,
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
        out = _spawn_detached(workers=args.workers, fresh=args.fresh)
        print(json.dumps(out, indent=2, default=str))
        return 0

    if args.foreground:
        if args.fresh:
            for name in (CK, REPORT):
                p = data_path(name)
                if p.exists():
                    p.unlink()

        job_path = data_path(JOB)
        job_meta = {
            "job_id": f"MDG-FG-{os.getpid()}",
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
                f"  {kw.get('pct', 0):3}% {kw.get('phase')} "
                f"done={kw.get('completed')} rem={kw.get('remaining')} eta={kw.get('eta_s')}",
                flush=True,
            )
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
            report = run_mfr_dist_graph_v1(resume=not args.fresh, workers=args.workers, on_progress=progress)
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
