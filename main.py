"""View contracts stored in PostgreSQL."""

import json
import sys
from datetime import date

from database import SessionLocal
from sync import list_contracts, sync_from_sam


def main() -> None:
    if "--sync" in sys.argv:
        result = sync_from_sam()
        print(f"Synced ({result['api_calls']} API call). {result['fetch_status']}\n")

    session = SessionLocal()
    try:
        contracts = list_contracts(session)
    finally:
        session.close()

    print(f"Showing {len(contracts)} matching contract(s) from database\n")
    for i, c in enumerate(contracts, 1):
        days = (c.due_date - date.today()).days if c.due_date else None
        print(f"{i}. {c.title}")
        print(f"   Agency:    {c.agency}")
        print(f"   Location:  {c.location}")
        print(f"   NAICS:     {c.naics_code}")
        print(f"   Set-aside: {c.set_aside}")
        print(f"   Due:       {c.due_date} ({days} days left)" if c.due_date else "   Due:       unknown")
        print(f"   Status:    {c.status}")
        if c.link:
            print(f"   Link:      {c.link}")
        print()

    if "--json" in sys.argv:
        payload = [
            {
                "notice_id": c.notice_id,
                "title": c.title,
                "agency": c.agency,
                "location": c.location,
                "naics_code": c.naics_code,
                "set_aside": c.set_aside,
                "due_date": c.due_date.isoformat() if c.due_date else None,
                "link": c.link,
                "status": c.status,
            }
            for c in contracts
        ]
        print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
