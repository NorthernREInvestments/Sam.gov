"""Continue a PackageAcquisitionJob by id (bounded batches). Build 002E."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BATCH_SIZE = int(os.environ.get("M3_002E_BATCH_SIZE", "25"))
MAX_BATCHES = int(os.environ.get("M3_002E_MAX_BATCHES", "120"))
WALL_SECONDS = int(os.environ.get("M3_002E_WALL_SECONDS", "2400"))


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: continue_package_job.py <job_id>", file=sys.stderr)
        return 2
    job_id = UUID(sys.argv[1])
    from m3.bootstrap import init_database
    from m3.db import SessionLocal
    from m3.models import PackageAcquisitionJob
    from m3.research.population_acquire import job_metrics, process_job_batch

    init_database()
    t0 = time.time()
    db = SessionLocal()
    try:
        job = db.get(PackageAcquisitionJob, job_id)
        if not job:
            print(json.dumps({"ok": False, "error": "not_found"}))
            return 1
        batches = 0
        last = {}
        while batches < MAX_BATCHES and (time.time() - t0) < WALL_SECONDS:
            last = process_job_batch(db, job_id, batch_size=BATCH_SIZE)
            batches += 1
            if batches % 5 == 0 or last.get("done"):
                print(
                    f"continue_progress batch={batches} cursor={last.get('cursor')}/{last.get('total')} "
                    f"pending={last.get('pending')}",
                    flush=True,
                )
            if last.get("done"):
                break
            db.expire_all()
        db.refresh(job)
        out = {"ok": True, "batches": batches, "last": last, "metrics": job_metrics(db, job)}
        print(json.dumps(out, indent=2, default=str))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
