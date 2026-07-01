"""Diagnose why dashboard shows fewer contracts than expected."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from database import SessionLocal
from models import Contract
from pws_fields import contract_pws_missing
from screening_pipeline import has_attachments_ready, is_dashboard_ready, is_full_analysis_complete
from settings_store import get_min_days_until_due, get_naics_codes, get_scheduler_settings
from sync import list_contracts


def main() -> None:
    s = SessionLocal()
    today = date.today()
    naics = get_naics_codes()
    sched = get_scheduler_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    recent = s.query(Contract).filter(Contract.last_updated_at >= cutoff).count()
    new_24h = s.query(Contract).filter(Contract.first_seen_at >= cutoff).count()
    all_n = s.query(Contract).count()
    in_naics = s.query(Contract).filter(Contract.naics_code.in_(naics)).count()

    not_ready: dict[str, int] = {
        "no_attachments": 0,
        "pws_missing": 0,
        "analysis_incomplete": 0,
        "far_found": 0,
        "other": 0,
    }
    ready: list[Contract] = []
    for r in s.query(Contract).filter(Contract.naics_code.in_(naics)).all():
        if is_dashboard_ready(r):
            ready.append(r)
            continue
        if getattr(r, "subcontracting_limitation_check", None) == "FOUND":
            not_ready["far_found"] += 1
        elif not has_attachments_ready(r):
            not_ready["no_attachments"] += 1
        elif contract_pws_missing(r):
            not_ready["pws_missing"] += 1
        elif not is_full_analysis_complete(r.analysis if isinstance(r.analysis, dict) else {}, r):
            not_ready["analysis_incomplete"] += 1
        else:
            not_ready["other"] += 1

    min_days = get_min_days_until_due()
    shown = list_contracts(s, naics_codes=naics, min_days_until_due=min_days)
    shown0 = list_contracts(s, naics_codes=naics, min_days_until_due=0)

    print("=== SCHEDULER ===")
    print(sched)
    print()
    print("=== DATABASE (last 24h) ===")
    print(f"Total contracts: {all_n}")
    print(f"In enabled NAICS: {in_naics}")
    print(f"New in last 24h: {new_24h}")
    print(f"Updated in last 24h: {recent}")
    print()
    print("=== DASHBOARD ===")
    print(f"Dashboard-ready: {len(ready)}")
    print(f"Shown (min_days={min_days}): {len(shown)}")
    print(f"Shown (min_days=0): {len(shown0)}")
    print(f"Hidden by min_days filter: {len(shown0) - len(shown)}")
    print(f"Not-ready reasons: {not_ready}")
    print()
    print("=== READY ===")
    for r in ready:
        days = (r.due_date - today).days if r.due_date else None
        score = (r.analysis or {}).get("score")
        print(f"  {r.title[:55]:55} | due {r.due_date} ({days}d) | score {score}")
    print()
    print("=== STUCK SAMPLE (up to 10) ===")
    stuck = [
        r
        for r in s.query(Contract).filter(Contract.naics_code.in_(naics)).all()
        if not is_dashboard_ready(r)
    ][:10]
    for r in stuck:
        a = r.analysis or {}
        print(
            f"  {r.title[:40]:40} | stage={a.get('screening_stage')} | "
            f"attach={has_attachments_ready(r)} | pws={contract_pws_missing(r)} | "
            f"far={r.subcontracting_limitation_check} | seen={r.first_seen_at.date() if r.first_seen_at else '?'}"
        )
    s.close()


if __name__ == "__main__":
    main()
