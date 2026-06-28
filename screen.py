"""Screen contracts with Claude and save analysis to PostgreSQL."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from database import SessionLocal
from models import Contract
from intake import full_intake_contract, intake_pending

logger = logging.getLogger("govtracker.screening")

def screen_pending(limit: int = 5, force: bool = False, matching_only: bool = False) -> dict[str, Any]:
    """
    Full intake for contracts without analysis: description → attachments → PDFs → Claude summary.
    If matching_only=True, only process contracts that pass current dashboard filters.
    """
    result = intake_pending(limit=limit, matching_only=matching_only, force=force)
    session = SessionLocal()
    try:
        pending = session.query(Contract).filter(Contract.analysis.is_(None)).count()
    finally:
        session.close()
    return {
        "screened": result.get("screened", 0),
        "skipped_existing": result.get("skipped", 0),
        "errors": result.get("errors", []),
        "pending_remaining": pending,
        "intake": result,
    }


def screen_one(notice_id: str, force: bool = False) -> dict[str, Any]:
    session = SessionLocal()
    try:
        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise ValueError(f"Contract not found: {notice_id}")
        if row.analysis and not force:
            return {"notice_id": notice_id, "skipped": True, "analysis": row.analysis}

        result = full_intake_contract(row, force=force)
        if result.get("in_progress"):
            return result
        if result.get("reason") == "sam_budget":
            raise ValueError(result.get("message") or "SAM.gov daily budget reached.")
        if result.get("reason") == "screen_budget":
            session.commit()
            return result

        session.commit()
        return result
    finally:
        session.close()


def start_background_screening(batch_size: int = 5) -> None:
    """Legacy alias — runs full intake pipeline in the background."""
    from intake import start_background_intake

    start_background_intake(batch_size=batch_size)


def main() -> None:
    limit = 5
    force = "--force" in sys.argv
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        limit = int(sys.argv[idx + 1])

    args = [a for a in sys.argv[1:] if not a.startswith("-") and a != str(limit)]
    if args:
        result = screen_one(args[0], force=force)
        print(json.dumps(result, indent=2))
        return

    matching_only = "--all" not in sys.argv
    result = screen_pending(limit=limit, force=force, matching_only=matching_only)
    print(f"Screened {result['screened']} contract(s).")
    print(f"{result['pending_remaining']} still waiting for screening.")
    if result["errors"]:
        print("Errors:")
        for err in result["errors"]:
            print(f"  - {err}")


if __name__ == "__main__":
    main()
