"""Find open domains that validate for unresolved MPNs."""
from __future__ import annotations

import httpx
from exact_product_url_discovery.sweep import unresolved_items
from exact_product_url_discovery.validate_identity import validate_product_page
from open_web_product_discovery.adapters_platt import platt_suggest_urls
from public_price_search.search import fetch_page

# Candidate URL builders for open-ish domains
builders = []

def mega(mpn, mfr):
    slug = f"{(mfr or 'product').lower().replace(' ', '-')}-{mpn.lower()}"
    return [
        f"https://www.megadepot.com/product/{slug}",
        f"https://megadepot.com/product/{slug}",
    ]

def diesel(mpn, mfr):
    return [f"https://www.dieselpartsdirect.com/{mpn.lower()}"]

def rock_oil(mpn, mfr):
    if (mfr or "").lower().startswith("wix") or mpn in {"51515"}:
        return [f"https://www.rockauto.com/en/parts/wix,{mpn},oil+filter,5340"]
    return []

def gmes_klein(mpn, mfr):
    if "klein" in (mfr or "").lower():
        return [f"https://www.gmes.com/search.php?search_query={mpn}"]
    return []

def platt_only(mpn, mfr):
    return [r["url"] for r in platt_suggest_urls(mpn, manufacturer=mfr, limit=2)]

items = unresolved_items()
found = []
for it in items:
    mpn = it.get("mpn") or ""
    mfr = it.get("manufacturer") or ""
    bid = it["benchmark_id"]
    cands = []
    for fn in (platt_only, diesel, rock_oil, mega):
        try:
            cands.extend(fn(mpn, mfr))
        except Exception:
            pass
    # also try direct known patterns
    cands.extend([
        f"https://www.dieselpartsdirect.com/{mpn.lower().replace('/', '-')}",
    ])
    hit = None
    for u in dict.fromkeys(cands):  # dedupe
        if "/search" in u:
            continue
        v = validate_product_page(u, mpn=mpn, manufacturer=mfr, allow_browser=False)
        if v.get("identity_match"):
            hit = (u, v.get("title"))
            break
    if hit:
        found.append((bid, hit[0]))
        print("FOUND", bid, hit[0][:100])
    else:
        print("MISS", bid)

print("TOTAL", len(found))
for b,u in found:
    print(b, u)
