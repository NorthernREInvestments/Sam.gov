"""Run SAM.gov search and print matching contracts."""

import json
import sys

from sam_client import fetch_opportunities


def main() -> None:
    try:
        contracts, api_calls, status = fetch_opportunities()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("Copy .env.example to .env and add your SAM_GOV_API_KEY.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"SAM.gov request failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Used {api_calls} SAM.gov API call(s)")
    print(f"{status}\n")
    print(f"Showing {len(contracts)} matching contract(s)\n")
    for i, c in enumerate(contracts, 1):
        print(f"{i}. {c['title']}")
        print(f"   Agency:    {c.get('agency')}")
        print(f"   Location:  {c.get('location')}")
        print(f"   NAICS:     {c.get('naics_code')}")
        print(f"   Set-aside: {c.get('set_aside')}")
        print(f"   Due:       {c.get('due_date')} ({c.get('days_until_due')} days left)")
        if c.get("link"):
            print(f"   Link:      {c['link']}")
        print()

    if "--json" in sys.argv:
        print(json.dumps(contracts, indent=2))


if __name__ == "__main__":
    main()
