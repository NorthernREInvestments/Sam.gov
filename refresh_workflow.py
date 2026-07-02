"""Repair contracts that already have PDF bytes stored in PostgreSQL."""

from __future__ import annotations

import json
import sys
import time

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent / ".env")

from workflow_backfill_service import repair_all_stored_attachment_contracts, repair_contract


def main() -> None:
    if "--stored-only" in sys.argv or "--all-stored" in sys.argv:
        print("Repairing ONLY contracts with PDF bytes already in PostgreSQL (no SAM.gov).")
        t0 = time.time()
        stats = repair_all_stored_attachment_contracts()
        stats["elapsed_sec"] = round(time.time() - t0, 1)
        print(json.dumps(stats, indent=2, default=str))
        return

    if "--one" in sys.argv:
        from database import SessionLocal
        from attachment_storage import contract_ids_with_stored_pdfs, load_contract_for_repair

        ids = contract_ids_with_stored_pdfs()
        if not ids:
            print(json.dumps({"error": "no_contract_with_stored_pdfs"}))
            return
        prep = SessionLocal()
        try:
            row = load_contract_for_repair(prep, ids[0])
            if not row:
                print(json.dumps({"error": "contract_not_found"}))
                return
            print(f"Repairing {row.notice_id} (1 of {len(ids)} with stored PDFs)...")
            t0 = time.time()
            result = repair_contract(prep, row)
            result["elapsed_sec"] = round(time.time() - t0, 1)
            print(json.dumps(result, indent=2, default=str))
        finally:
            prep.close()
        return

    print("Usage: python refresh_workflow.py --stored-only")
    print("       python refresh_workflow.py --one")


if __name__ == "__main__":
    main()
