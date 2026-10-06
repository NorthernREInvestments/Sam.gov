"""Batch probe Platt with pacing + raw diagnostics."""
from __future__ import annotations

import json
import time

from exact_product_url_discovery.sweep import unresolved_items
from open_web_product_discovery.adapters_platt import platt_suggest_urls
import httpx
from open_web_product_discovery.adapters_platt import _UA, _guest_id, _gql, _SUGGEST_Q, _cat_match, _mfr_match

items = unresolved_items()
print("sample item keys", sorted(items[0].keys()))
print("sample", {k: items[0].get(k) for k in ("benchmark_id", "manufacturer", "mpn", "part_number")})

# raw suggest for a few known
with httpx.Client(timeout=25.0, headers=_UA, follow_redirects=True) as c:
    cid = _guest_id(c)
    for q in ["430", "Channellock 430", "11055", "Klein 11055", "5320-S", "TH8320U1008"]:
        data = _gql(c, _SUGGEST_Q, {"bannerCode": "PLATT", "customerId": cid, "query": q})
        rows = (
            ((((data.get("data") or {}).get("viewer") or {}).get("customerById") or {}).get("suggest") or {}).get("all")
            or []
        )
        print(f"\nQ={q!r} n={len(rows)}")
        for r in rows[:4]:
            if r.get("__typename") != "ProductSuggestResponseLineProduct":
                print(" ", r.get("__typename"), r.get("text"))
                continue
            s = ((r.get("product") or {}).get("summary") or {})
            print(" ", s.get("catNum"), (s.get("manufacturer") or {}).get("name"), s.get("title"), s.get("urlInternal"))

hits = 0
for i, it in enumerate(items):
    mpn = it.get("mpn") or it.get("part_number") or ""
    mfr = it.get("manufacturer") or ""
    rows = platt_suggest_urls(mpn, manufacturer=mfr, limit=2)
    if rows:
        hits += 1
        print("HIT", it["benchmark_id"], rows[0]["url"][:100])
    else:
        print("MISS", it["benchmark_id"], mfr, mpn)
    time.sleep(0.35)
print("hits", hits, "/", len(items))
