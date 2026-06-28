"""Screen contracts with Claude and save analysis to PostgreSQL."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

from claude_client import screen_contract
from database import SessionLocal
from models import Contract


def screen_pending(limit: int = 5, force: bool = False, matching_only: bool = False) -> dict[str, Any]:
    """
    Screen contracts without analysis.
    If matching_only=True, only screen contracts that pass current dashboard filters.
    """
    from sync import list_contracts

    session = SessionLocal()
    screened = 0
    skipped = 0
    errors: list[str] = []

    try:
        if matching_only:
            rows = list_contracts(session)
            if not force:
                rows = [r for r in rows if not r.analysis]
            rows = rows[:limit]
        else:
            query = session.query(Contract).order_by(Contract.first_seen_at.desc())
            if not force:
                query = query.filter(Contract.analysis.is_(None))
            rows = query.limit(limit).all()

        for row in rows:
            if row.analysis and not force:
                skipped += 1
                continue
            try:
                analysis = screen_contract(row)
                row.analysis = analysis
                row.last_updated_at = datetime.now(timezone.utc)
                if analysis.get("pursue") is False:
                    row.status = "skipped"
                elif row.status == "new":
                    row.status = "reviewing"
                if analysis.get("estimated_value") and not row.estimated_value:
                    row.estimated_value = str(analysis["estimated_value"])[:128]
                session.commit()
                screened += 1
            except Exception as exc:
                session.rollback()
                errors.append(f"{row.notice_id}: {exc}")

        pending = session.query(Contract).filter(Contract.analysis.is_(None)).count()
    finally:
        session.close()

    return {
        "screened": screened,
        "skipped_existing": skipped,
        "errors": errors,
        "pending_remaining": pending,
    }


def screen_one(notice_id: str, force: bool = False) -> dict[str, Any]:
    session = SessionLocal()
    try:
        row = session.query(Contract).filter_by(notice_id=notice_id).first()
        if not row:
            raise ValueError(f"Contract not found: {notice_id}")
        if row.analysis and not force:
            return {"notice_id": notice_id, "skipped": True, "analysis": row.analysis}

        analysis = screen_contract(row)
        row.analysis = analysis
        row.last_updated_at = datetime.now(timezone.utc)
        if analysis.get("pursue") is False:
            row.status = "skipped"
        elif row.status in ("new", "skipped"):
            row.status = "reviewing"
        if analysis.get("estimated_value") and not row.estimated_value:
            row.estimated_value = str(analysis["estimated_value"])[:128]
        session.commit()
        return {"notice_id": notice_id, "skipped": False, "analysis": analysis}
    finally:
        session.close()


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
