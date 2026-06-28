"""Clear all contracts and sync rotation state for a fresh rescrape."""

from __future__ import annotations

import argparse

from database import SessionLocal
from models import AppSetting, Contract


def clear_contracts(*, reset_naics_sync: bool = True) -> dict[str, int]:
    session = SessionLocal()
    try:
        deleted = session.query(Contract).delete()
        if reset_naics_sync:
            for key in ("naics_last_synced", "naics_rotation_index"):
                row = session.get(AppSetting, key)
                if row:
                    session.delete(row)
        session.commit()
        return {"contracts_deleted": deleted}
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Clear GovTracker contracts for a fresh SAM.gov rescrape.")
    parser.add_argument(
        "--keep-naics-rotation",
        action="store_true",
        help="Keep NAICS sync rotation settings (default: reset rotation)",
    )
    args = parser.parse_args()

    result = clear_contracts(reset_naics_sync=not args.keep_naics_rotation)
    print(f"Deleted {result['contracts_deleted']} contract(s) from PostgreSQL.")
    if not args.keep_naics_rotation:
        print("Reset NAICS sync rotation.")


if __name__ == "__main__":
    main()
