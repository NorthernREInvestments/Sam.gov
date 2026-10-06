"""Validate curated non-shell candidates for the 44 unresolved items."""
from __future__ import annotations

from exact_product_url_discovery.classify import is_search_shell
from exact_product_url_discovery.sweep import unresolved_items
from exact_product_url_discovery.validate_identity import validate_product_page
from manufacturer_distributor_graph.exact_urls import curated_exact_urls

items = unresolved_items()
ok = []
fail = []
for it in items:
    mpn = it.get("mpn") or ""
    mfr = it.get("manufacturer") or ""
    bid = it["benchmark_id"]
    urls = [u for u in curated_exact_urls(mpn) if u.startswith("http") and not is_search_shell(u)]
    found = None
    reasons = []
    for u in urls[:4]:
        v = validate_product_page(u, mpn=mpn, manufacturer=mfr, description=it.get("description"), allow_browser=False)
        if v.get("identity_match"):
            found = (u, v.get("via"), v.get("title"))
            break
        reasons.append((u[:80], v.get("reason")))
    if found:
        ok.append((bid, found[0], found[1]))
        print("OK", bid, found[0][:100])
    else:
        fail.append((bid, mfr, mpn, reasons[:2]))
        print("FAIL", bid, mfr, mpn, reasons[:1])

print("---")
print("ok", len(ok), "fail", len(fail))
