"""Start / status hard-miss recovery + denominator audit.

  python scripts/run_hard_miss_recovery_v1.py --freeze
  python scripts/run_hard_miss_recovery_v1.py --audit-only
  python scripts/run_hard_miss_recovery_v1.py --start
  python scripts/run_hard_miss_recovery_v1.py --foreground
  python scripts/run_hard_miss_recovery_v1.py --status
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


def _spawn(*, fresh: bool, skip_audit: bool) -> dict:
    from hard_miss_recovery.models import BUILD, CK, JOB, REPORT
    from m3_data_root import data_path

    log_path = data_path("m3_hard_miss_recovery_v1_worker.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--foreground"]
    if fresh:
        cmd.append("--fresh")
    if skip_audit:
        cmd.append("--skip-audit")
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
        "job_id": f"HMR-DETACHED-{proc.pid}",
        "build": BUILD,
        "status": "RUNNING",
        "pid": proc.pid,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "result_path": REPORT,
        "checkpoint_path": CK,
        "log_path": str(log_path),
        "mode": "detached_subprocess",
    }
    data_path(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
    return {"ok": True, "job_id": job["job_id"], "pid": proc.pid, "log_path": str(log_path)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--audit-only", action="store_true")
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--foreground", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--skip-audit", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    from hard_miss_recovery.audit import run_denominator_audit
    from hard_miss_recovery.corpus import build_hard_miss_corpus
    from hard_miss_recovery.models import BUILD, CK, FROZEN, JOB, PRICE_FOUND, REPORT
    from hard_miss_recovery.sweep import build_final_report, format_completion_report, run_hard_miss_recovery_v1
    from m3_data_root import data_path

    if args.freeze:
        payload = build_hard_miss_corpus(force=True)
        print(json.dumps({"ok": True, "n_misses": payload.get("n_misses")}, indent=2))
        return 0

    if args.audit_only:
        audit = run_denominator_audit(force=True)
        print(
            json.dumps(
                {
                    "ok": True,
                    "original": audit.get("original_denominator"),
                    "audited": audit.get("audited_denominator"),
                    "changed": audit.get("denominator_changed"),
                    "counts": audit.get("counts"),
                    "removed": len(audit.get("removed_or_reclassified") or []),
                },
                indent=2,
            )
        )
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
                        "priced": sum(
                            1
                            for v in items.values()
                            if v.get("status") in {PRICE_FOUND, FROZEN} and (v.get("found") or {}).get("usable")
                        ),
                        "report_ready": ck.get("report_ready"),
                        "full100_original": ck.get("full100_original"),
                        "full100_audited": ck.get("full100_audited"),
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

    if args.report:
        report = build_final_report()
        print(format_completion_report(report))
        print(json.dumps(report.get("FINAL_ANSWERS"), indent=2, default=str))
        return 0

    if args.start and not args.foreground:
        out = _spawn(fresh=args.fresh, skip_audit=args.skip_audit)
        print(json.dumps(out, indent=2))
        return 0

    if args.foreground or args.start:
        print(f"govtracker: hard-miss recovery foreground ({BUILD})", flush=True)
        job = {
            "job_id": f"HMR-FG-{os.getpid()}",
            "build": BUILD,
            "status": "RUNNING",
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        data_path(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
        try:
            report = run_hard_miss_recovery_v1(fresh=args.fresh, skip_audit=args.skip_audit, audit_force=True)
            job["status"] = "COMPLETE"
            job["SAFE_TO_SCALE"] = (report.get("SCALE_DECISION") or {}).get("SAFE_TO_SCALE")
            job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            data_path(JOB).write_text(json.dumps(job, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "ok": True,
                        "SAFE_TO_SCALE": job["SAFE_TO_SCALE"],
                        "FULL100": report.get("FULL100"),
                        "EASY25": report.get("EASY25"),
                        "FINAL_ANSWERS": report.get("FINAL_ANSWERS"),
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
