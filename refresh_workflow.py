"""Repair all contracts through the correct workflow order (no manual force)."""

from __future__ import annotations

import json
import sys

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

from workflow_backfill_service import run_workflow_repair_until_idle


def main() -> None:
    batch_size = 5
    if "--batch" in sys.argv:
        idx = sys.argv.index("--batch")
        batch_size = int(sys.argv[idx + 1])
    totals = run_workflow_repair_until_idle(batch_size=batch_size)
    print(json.dumps(totals, indent=2))


if __name__ == "__main__":
    main()
