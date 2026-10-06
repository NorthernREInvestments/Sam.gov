"""Start / status seller rediscovery job (staged A→B→C).

  python scripts/run_seller_rediscovery_v1.py --freeze
  python scripts/run_seller_rediscovery_v1.py --stage A --foreground
  python scripts/run_seller_rediscovery_v1.py --start --stages A,B,C
  python scripts/run_seller_rediscovery_v1.py --status
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


def _spawn(*, stages: str, fresh: bool) -> dict:
    from m3_data_root import data_path
    from seller_rediscovery.models import BUILD, CK, JOB, REPORT

    log_path = data_path("m3_seller_rediscovery_v1_worker.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--foreground",
        "--stages",
        stages,
    ]
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
        "job_id": f"SR-DETACHED-{proc.pid}",
        "build": BUILD,
        "status": "RUNNING",
        "pid": proc.pid,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "progress": {"phase": "SPAWNED", "pct": 0},
        "stages": stages,
        "result_path": REPORT,
        "checkpoint_path": CK,
        "log_path": str(log_path),
        "mode": "detached_subprocess",
    }
    data_path(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
    return {"ok": True, "job_id": job["job_id"], "pid": proc.pid, "log_path": str(log_path), "job": job}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--foreground", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--stage", type=str, default="")
    ap.add_argument("--stages", type=str, default="A,B,C")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    from m3_data_root import data_path
    from seller_rediscovery.freeze import freeze_validated_baseline
    from seller_rediscovery.models import BUILD, CK, FROZEN, JOB, PRICE_FOUND, REPORT
    from seller_rediscovery.sweep import (
        build_final_report,
        format_completion_report,
        run_seller_rediscovery_v1,
        run_stage,
    )

    if args.freeze:
        payload = freeze_validated_baseline(force=True)
        print(json.dumps({"ok": True, "n_frozen": payload.get("n_frozen"), "acceptance": payload.get("acceptance")}, indent=2))
        return 0

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
                        "priced": sum(
                            1
                            for v in items.values()
                            if v.get("status") in {PRICE_FOUND, FROZEN} and (v.get("found") or {}).get("usable")
                        ),
                        "report_ready": ck.get("report_ready"),
                        "full100": ck.get("full100"),
                        "easy25": ck.get("easy25"),
                        "stages": {k: {"new": v.get("new_recoveries"), "processed": v.get("processed")} for k, v in (ck.get("stages") or {}).items()},
                    },
                },
                indent=2,
                default=str,
            )
        )
        if data_path(REPORT).exists() and ck.get("report_ready"):
            print("\n" + format_completion_report(json.loads(data_path(REPORT).read_text(encoding="utf-8"))))
        return 0

    if args.report:
        report = build_final_report()
        print(format_completion_report(report))
        print(json.dumps(report.get("FINAL_ANSWERS"), indent=2, default=str))
        return 0

    stages = [s.strip().upper() for s in (args.stage or args.stages).split(",") if s.strip()]
    if args.start and not args.foreground:
        out = _spawn(stages=",".join(stages), fresh=args.fresh)
        print(json.dumps(out, indent=2))
        return 0

    if args.foreground or args.stage or args.stages:
        print(f"govtracker: seller rediscovery foreground ({BUILD}) stages={stages}", flush=True)
        from m3_data_root import data_path as dp
        from seller_rediscovery.models import JOB as JOB_NAME

        job = {
            "job_id": f"SR-FG-{os.getpid()}",
            "build": BUILD,
            "status": "RUNNING",
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stages": stages,
        }
        dp(JOB_NAME).write_text(json.dumps(job, indent=2), encoding="utf-8")
        try:
            report = run_seller_rediscovery_v1(stages=stages, fresh=args.fresh)
            job["status"] = "COMPLETE"
            job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            job["SAFE_TO_SCALE"] = (report.get("ACCEPTANCE") or {}).get("SAFE_TO_SCALE")
            dp(JOB_NAME).write_text(json.dumps(job, indent=2), encoding="utf-8")
            print(json.dumps({"ok": True, "SAFE_TO_SCALE": job["SAFE_TO_SCALE"], "FULL100": report.get("FULL100"), "EASY25": report.get("EASY25")}, indent=2))
            return 0
        except Exception as exc:
            job["status"] = "FAILED"
            job["error"] = str(exc)
            dp(JOB_NAME).write_text(json.dumps(job, indent=2), encoding="utf-8")
            raise

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
