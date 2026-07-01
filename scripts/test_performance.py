"""Smoke test performance tracking APIs."""

from __future__ import annotations

from database import SessionLocal, init_db
from models import Contract
from performance_service import (
    create_invoice,
    get_contract_performance,
    performance_dashboard,
    update_contract_performance,
)


def main() -> None:
    init_db()
    session = SessionLocal()
    try:
        row = session.query(Contract).first()
        if not row:
            print("No contracts in DB — skip")
            return
        nid = row.notice_id
        print(f"Testing contract {nid}")
        update_contract_performance(
            session,
            nid,
            {
                "status": "active",
                "government_contract_number": "TEST-12345",
                "option_years_remaining": 2,
                "invoicing_system": "WAWF",
            },
        )
        inv = create_invoice(
            session,
            nid,
            {
                "billing_period_start": "2026-06-01",
                "billing_period_end": "2026-06-30",
                "invoice_amount": 5000,
            },
        )
        session.commit()
        perf = get_contract_performance(session, nid)
        dash = performance_dashboard(session)
        print("Invoice:", inv.invoice_number, perf["current_invoice"]["status"] if perf.get("current_invoice") else "none")
        print("Dashboard active:", dash["summary"]["active_contracts"])
        print("OK")
    finally:
        session.close()


if __name__ == "__main__":
    main()
