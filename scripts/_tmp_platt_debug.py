"""Debug Platt GraphQL."""
from __future__ import annotations

import json

from open_web_product_discovery.adapters_platt import platt_suggest_urls, _guest_id, _gql, _CUSTOMERS_Q, _SUGGEST_Q, _UA
import httpx

print("direct adapter:", platt_suggest_urls("11055", manufacturer="Klein", limit=3))
print("channellock:", platt_suggest_urls("430", manufacturer="Channellock", limit=3))

with httpx.Client(timeout=25.0, headers=_UA, follow_redirects=True) as c:
    cid = _guest_id(c)
    print("guest", cid)
    if cid:
        data = _gql(c, _SUGGEST_Q, {"bannerCode": "PLATT", "customerId": cid, "query": "11055"})
        rows = (
            ((((data.get("data") or {}).get("viewer") or {}).get("customerById") or {}).get("suggest") or {}).get("all")
            or []
        )
        print("raw rows", len(rows))
        for r in rows[:5]:
            print(json.dumps(r, indent=2)[:500])
        if data.get("errors"):
            print("errors", data["errors"])
