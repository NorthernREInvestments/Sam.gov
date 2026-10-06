"""Browser-probe known PDPs for visible/network prices."""
from __future__ import annotations

import json
import re
from price_adapters.browser import bounded_browser_fetch
from price_adapters.validate import seller_of

URLS = [
    ("AC216CVS", "https://www.acmetools.com/crescent-ac216cvs-adjustable-wrench.html"),
    ("B-45580", "https://www.toolbarn.com/makita-b-45580/"),
    ("121943", "https://www.seton.com/brady-danger-do-not-operate-tag-b906.html"),
    ("05005", "https://crcautocare.com/product/crc-brakleen-brake-parts-cleaner-non-chlorinated-14-oz-05005/"),
    ("9290018191", "https://www.1000bulbs.com/product/218154/PHILIPS-9290018191.html"),
    ("DWHT56027", "https://www.acmetools.com/dewalt-dwht56027-folding-jab-saw.html"),
]

for mpn, u in URLS:
    print("===", mpn, seller_of(u))
    br = bounded_browser_fetch(u, timeout_ms=20000, wait_ms=3000, capture_json=True)
    print(" ok", br.get("ok"), "cached", br.get("cached"), "err", br.get("error"), "json_urls", br.get("json_urls"))
    html = br.get("html") or ""
    text = br.get("text") or ""
    print(" html_len", len(html), "text_len", len(text))
    m = re.search(r"<title[^>]*>([^<]{0,140})", html, re.I)
    print(" title", (m.group(1).strip() if m else "")[:110])
    # visible $ in text
    dollars = re.findall(r"\$\s*(\d{1,5}(?:\.\d{2})?)", text[:20000])
    print(" dollars_in_text", dollars[:12])
    prices = re.findall(r'"price"\s*:\s*"?(\d+\.?\d*)"?', html[:400000], flags=re.I)[:12]
    print(" price_json", prices)
    for needle in ("__NEXT_DATA__", "application/ld+json", "shopify", "itemprop=\"price\"", "data-price"):
        if needle.lower() in html.lower():
            print(" has", needle)
    print()
