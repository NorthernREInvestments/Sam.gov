"""Build MegaDepot search conversion + expand open curated URLs."""
from __future__ import annotations

import re
from urllib.parse import quote_plus, urljoin

from public_price_search.search import fetch_page
from exact_product_url_discovery.validate_identity import validate_product_page
from exact_product_url_discovery.classify import is_search_shell
from exact_product_url_discovery.normalize import mpn_in_text
from exact_product_url_discovery.sweep import unresolved_items


def mega_search(mpn: str, manufacturer: str | None = None, limit: int = 5):
    q = f"{manufacturer or ''} {mpn}".strip()
    url = f"https://megadepot.com/?s={quote_plus(q)}"
    fr = fetch_page(url)
    if not fr.get("ok"):
        return []
    html = fr.get("text") or ""
    base = str(fr.get("url") or url)
    out = []
    seen = set()
    for m in re.finditer(r'href=["\']([^"\']+/product/[^"\']+)["\']', html, re.I):
        u = urljoin(base, m.group(1))
        if u in seen or is_search_shell(u):
            continue
        if not mpn_in_text(mpn, u):
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= limit:
            break
    return out


items = unresolved_items()
found = []
for it in items:
    mpn = it.get("mpn") or ""
    mfr = it.get("manufacturer") or ""
    bid = it["benchmark_id"]
    urls = mega_search(mpn, mfr, limit=4)
    hit = None
    for u in urls:
        v = validate_product_page(u, mpn=mpn, manufacturer=mfr, description=it.get("description"), allow_browser=False)
        if v.get("identity_match"):
            hit = (u, v.get("title"))
            break
        # also try without description product check
    if hit:
        found.append((bid, hit[0], hit[1]))
        print("FOUND", bid, hit[0][:110])
    else:
        print("MISS", bid, "cands", len(urls), urls[:1])

print("TOTAL", len(found))
