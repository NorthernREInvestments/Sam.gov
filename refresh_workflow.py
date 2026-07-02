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
    if "--one" in sys.argv:
        from database import SessionLocal
        from models import Contract, ContractAttachment
        from workflow_backfill_service import repair_contract

        session = SessionLocal()
        try:
            row = (
                session.query(Contract)
                .join(ContractAttachment, ContractAttachment.contract_id == Contract.id)
                .filter(ContractAttachment.file_bytes.isnot(None))
                .order_by(Contract.id)
                .first()
            )
            if not row:
                print(json.dumps({"error": "no_contract_with_stored_pdfs"}))
                return
            prep = SessionLocal()
            try:
                result = repair_contract(prep, row)
            finally:
                prep.close()
            print(json.dumps(result, indent=2, default=str))
        finally:
            session.close()
        return

    totals = run_workflow_repair_until_idle(batch_size=batch_size)
    print(json.dumps(totals, indent=2))


if __name__ == "__main__":
    main()
